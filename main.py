# -*- coding: utf-8 -*-
# =============================================================================
#  DMCleanerWT  ·  Code by Witcruh & Taz
#  Kendi Discord DM mesajlarını toplu ve kontrollü şekilde silen masaüstü aracı.
#
#  Tüm uygulama bu tek dosyadadır (bölümler: 1 Motor · 2 Animasyon · 3 Arka plan · 4 Arayüz).
#
#  !!! ToS UYARISI !!!
#  Bu araç kullanıcı token'ı ("self-token") ile çalışır. Kullanıcı hesaplarının otomatikleştirilmesi
#  (self-bot) Discord Hizmet Şartları'na AYKIRIDIR; hesabın kısıtlanabilir veya kapatılabilir.
#  Yalnızca KENDİ hesabında, riski kabul ederek kullan. Silinen mesajlar GERİ GETİRİLEMEZ.
#  Bu araç yalnızca SENİN gönderdiğin mesajları siler.
# =============================================================================

import asyncio
import base64
import ctypes
import io
import json
import logging
import math
import os
import queue
import random
import re
import sys
import threading
import time
import tkinter as tk
import traceback
import urllib.request
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
from tkinter import filedialog, messagebox
from tkinter import font as tkfont

import customtkinter as ctk

# discord.py-self de "discord" adıyla import edilir; normal discord.py ile çakışır (arayüzde uyarılır).
try:
    import discord
    DISCORD_IMPORT_ERROR = None
except Exception as exc:  # noqa: BLE001
    discord = None
    DISCORD_IMPORT_ERROR = exc

# Pillow isteğe bağlıdır: yoksa/arıza varsa arka plan görseli atlanır, uygulama koyu zeminle çalışır.
try:
    from PIL import Image, ImageOps
except Exception:  # noqa: BLE001
    Image = ImageOps = None
try:
    from PIL import ImageTk
except Exception:  # noqa: BLE001
    ImageTk = None


# =============================================================================
# 1) MOTOR
# =============================================================================
APP_NAME = "DMCleanerWT"
APP_TITLE = "DMCleanerWT"
CREDIT = "Code by Witcruh & Taz"
LEGACY_APP_NAME = "DMTemizleyici"   # yalnızca eski ayarları (kayıtlı token) taşımak için okunur


def resource_path(rel: str) -> str:
    """Kaynak dosya yolu: PyInstaller (--onefile) açılışta dosyaları sys._MEIPASS'e çıkarır; geliştirmede script klasörü.
    Çalışma dizininden (cwd) bağımsızdır."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, *rel.replace("\\", "/").split("/"))


# Hız ön ayarları: (ad, silmeler arası TABAN bekleme sn, açıklama). Değerler gerçek hesapta ÖLÇÜLMEDİ;
# yalnızca taban süredir. Discord 429 verirse süre otomatik artar ve sunucunun Retry-After değeri her zaman uygulanır.
SPEED_PRESETS = [
    ("Kontrollü", 2.0, "Her silmeden sonra en az 2.0 sn bekler. Daha yavaş; istek sayısını düşük tutar."),
    ("Dengeli", 1.5, "Varsayılan. Önceki sürümün kullandığı 1.5 sn taban bekleme."),
    ("Hızlı", 1.0, "En az 1.0 sn bekler. Gerçek hesapta test edilmedi; 429 daha sık görülebilir."),
    ("Çok hızlı", 0.6, "En az 0.6 sn bekler. Gerçek hesapta test edilmedi; 429 ve geçici kısıt riski daha yüksek."),
    ("Özel", None, "Kendi bekleme süreni gir (en az 0.3 sn). Güvenlik garantisi yoktur."),
]
DEFAULT_SPEED = "Dengeli"
MIN_DELAY = 0.3


# -----------------------------------------------------------------------------
# Dosya yolları
# -----------------------------------------------------------------------------
def _config_dir(name: str = None) -> str:
    root = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(root, name or APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def _writable_dir() -> str:
    """Log klasörü: önce exe/script'in yanı, yazılamıyorsa APPDATA."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    try:
        test = os.path.join(base, ".write_test")
        with open(test, "w", encoding="utf-8"):
            pass
        os.remove(test)
        return base
    except OSError:
        return _config_dir()


BASE_DIR = _writable_dir()
APP_LOG = os.path.join(BASE_DIR, "app.log")
DELETED_LOG = os.path.join(BASE_DIR, "silinen_mesajlar.log")
SUMMARY_FILE = os.path.join(BASE_DIR, "son_islem.json")
CONFIG_FILE = os.path.join(_config_dir(), "config.json")


# -----------------------------------------------------------------------------
# Loglama (token asla log'a yazılmaz)
# -----------------------------------------------------------------------------
class _TokenScrubber(logging.Filter):
    """Kayıtlı sırları (token) log satırlarından siler."""

    def __init__(self):
        super().__init__()
        self._secrets: set = set()

    def add(self, secret: str):
        if secret and len(secret) >= 8:
            self._secrets.add(secret)

    def filter(self, record):
        try:
            msg = record.getMessage()
            for s in self._secrets:
                if s in msg:
                    msg = msg.replace(s, "***")
            record.msg, record.args = msg, ()
        except Exception:  # noqa: BLE001
            pass
        return True


SCRUBBER = _TokenScrubber()
logging.basicConfig(
    filename=APP_LOG,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    encoding="utf-8",
)
for _h in logging.getLogger().handlers:
    _h.addFilter(SCRUBBER)
log = logging.getLogger(APP_NAME)


class _RateLimitWatcher(logging.Handler):
    """discord.py-self 429'ları kendi içinde sessizce tekrar dener (exception atmaz).
    Kütüphanenin 'rate limited' log satırlarını sayarak 429 olduğunu anlıyoruz."""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self._lock = threading.Lock()
        self.count = 0

    def emit(self, record):
        try:
            if "rate limited" in record.getMessage():
                with self._lock:
                    self.count += 1
        except Exception:  # noqa: BLE001
            pass


RATE_WATCHER = _RateLimitWatcher()
logging.getLogger("discord.http").addHandler(RATE_WATCHER)


def _excepthook(exc_type, exc, tb):
    log.error("Yakalanmamış hata:\n%s", "".join(traceback.format_exception(exc_type, exc, tb)))
    if sys.stderr is not None:
        sys.__excepthook__(exc_type, exc, tb)


sys.excepthook = _excepthook
threading.excepthook = lambda a: _excepthook(a.exc_type, a.exc_value, a.exc_traceback)


# -----------------------------------------------------------------------------
# Token saklama: Windows DPAPI -> yoksa XOR + Base64 (orijinalden korundu)
# -----------------------------------------------------------------------------
_XOR_KEY = b"dm-temizleyici::obf::v1"


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, encrypt: bool) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _DataBlob(len(data), buf)
    blob_out = _DataBlob()
    func = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    if not func(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError("DPAPI çağrısı başarısız")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


def _xor(data: bytes) -> bytes:
    return bytes(b ^ _XOR_KEY[i % len(_XOR_KEY)] for i, b in enumerate(data))


def protect_token(token: str) -> str:
    raw = token.encode("utf-8")
    if sys.platform == "win32":
        try:
            return "dpapi:" + base64.b64encode(_dpapi(raw, True)).decode("ascii")
        except Exception:  # noqa: BLE001
            log.warning("DPAPI kullanılamadı, XOR obfuscation'a düşülüyor")
    return "xor:" + base64.b64encode(_xor(raw)).decode("ascii")


def unprotect_token(stored: str) -> str:
    try:
        kind, _, payload = stored.partition(":")
        raw = base64.b64decode(payload)
        if kind == "dpapi":
            return _dpapi(raw, False).decode("utf-8")
        if kind == "xor":
            return _xor(raw).decode("utf-8")
    except Exception:  # noqa: BLE001
        log.warning("Kayıtlı token çözülemedi, yok sayılıyor")
    return ""


def load_config() -> dict:
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        pass
    # Eski sürümün ayarlarını (örn. kayıtlı token) bir kez devral
    legacy = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), LEGACY_APP_NAME, "config.json")
    try:
        with open(legacy, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError:
        log.exception("Ayarlar kaydedilemedi")


# -----------------------------------------------------------------------------
# Girdi doğrulama (saf fonksiyonlar, test edilebilir)
# -----------------------------------------------------------------------------
def validate_user_id(text: str) -> int:
    """Discord snowflake ID: yalnızca rakam, 17-20 hane."""
    s = (text or "").strip()
    if not s:
        raise ValueError("Kullanıcı ID boş olamaz.")
    if not s.isdigit():
        raise ValueError("Kullanıcı ID yalnızca rakamlardan oluşmalı.")
    if not 17 <= len(s) <= 20:
        raise ValueError("Kullanıcı ID 17-20 haneli olmalı (Discord'da Geliştirici Modu ile kopyala).")
    return int(s)


_DATE_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$")
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def _parse_dt(date_s: str, time_s: str, default_time: str, tz, label: str) -> datetime:
    m = _DATE_RE.match(date_s.strip())
    if not m:
        raise ValueError(f"{label} tarihi GG.AA.YYYY biçiminde olmalı (örn. 31.12.2025).")
    t = (time_s or "").strip() or default_time
    tm = _TIME_RE.match(t)
    if not tm:
        raise ValueError(f"{label} saati SS:DD biçiminde olmalı (örn. 18:30).")
    day, month, year = int(m[1]), int(m[2]), int(m[3])
    hour, minute = int(tm[1]), int(tm[2])
    try:
        naive = datetime(year, month, day, hour, minute)
    except ValueError:
        raise ValueError(f"{label}: geçersiz tarih veya saat.") from None
    return naive.replace(tzinfo=tz) if tz is not None else naive.astimezone()


def parse_range(start_date: str, start_time: str, end_date: str, end_time: str, tz=None):
    """Yerel saat girdilerini UTC (after, before) çiftine çevirir.
    Saat boşsa başlangıç 00:00, bitiş 23:59 (dakikanın sonu dahil) alınır.
    En az bir sınır zorunlu; ikisi de varsa başlangıç < bitiş olmalı."""
    if not (start_date or "").strip() and not (end_date or "").strip():
        raise ValueError("En az bir tarih (başlangıç veya bitiş) girmelisin.")
    after = before = None
    if (start_date or "").strip():
        after = _parse_dt(start_date, start_time, "00:00", tz, "Başlangıç").astimezone(timezone.utc)
    if (end_date or "").strip():
        end = _parse_dt(end_date, end_time, "23:59", tz, "Bitiş")
        if not (end_time or "").strip():
            end = end.replace(second=59, microsecond=999999)
        before = end.astimezone(timezone.utc)
    if after and before and after >= before:
        raise ValueError("Başlangıç tarihi bitiş tarihinden önce olmalı.")
    return after, before


def local_tz_label() -> str:
    off = datetime.now().astimezone().strftime("%z")
    return f"UTC{off[:3]}:{off[3:]}" if off else "UTC"


def fmt_duration(seconds: float) -> str:
    s = int(max(0, seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}"


# -----------------------------------------------------------------------------
# Silme işçisi: kendi thread'inde, kendi asyncio döngüsünde çalışır
# -----------------------------------------------------------------------------
class NetworkDown(Exception):
    """Yeniden denemeler tükendi: internet bağlantısı kesik."""


class Purger:
    """İki aşamalı: (1) bağlan + hesabı doğrula, (2) kullanıcı onaylayınca `begin()` ile sil.
    GUI ile yalnızca `events` kuyruğu üzerinden konuşur."""

    BATCH = 25
    MAX_DELAY = 15.0

    def __init__(self, token: str, delay: float, events: queue.Queue):
        self.token = token
        SCRUBBER.add(token)
        self.events = events
        self.opt: dict = {}
        self.stop_flag = threading.Event()
        self.loop = None
        self.client = None
        self.thread = threading.Thread(target=self._thread_main, daemon=True)
        self.deleted = 0
        self.scanned = 0
        self.failed = 0
        self.rl_hits = 0
        self.last_action = ""
        self.state = "bağlanıyor"
        self._job_started = False
        self._go = None            # asyncio.Event: onay verildi
        self._done_channels: list = []
        self._started_at = None
        self._deleted_fh = None
        self.base_delay = delay
        self.cur_delay = delay
        self._clean_streak = 0

    # --- GUI tarafından çağrılanlar -------------------------------------------
    def start(self):
        self.thread.start()

    def request_preview(self, opt: dict):
        """Seçilen kapsamın tahmini (kanal sayısı / hedef kullanıcı) -> ('preview', dict)."""
        loop = self.loop
        if loop and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._compute_preview, dict(opt))
            except RuntimeError:
                pass

    def begin(self, opt: dict):
        """Kullanıcı onayladı: silmeyi başlat. Bu çağrıdan önce hiçbir mesaj silinmez."""
        self.opt = dict(opt)
        if opt.get("delay") is not None:
            self.base_delay = self.cur_delay = max(MIN_DELAY, float(opt["delay"]))
        loop = self.loop
        if loop and not loop.is_closed() and self._go is not None:
            loop.call_soon_threadsafe(self._go.set)

    def stop(self):
        """Durdurma: yeni silme isteği gönderilmez; bağlantı/onay aşamasındaysa istemci kapatılır."""
        self.stop_flag.set()
        loop, client = self.loop, self.client
        if loop and not loop.is_closed():
            try:
                if self._go is not None:
                    loop.call_soon_threadsafe(self._go.set)
                if client and not self._job_started:
                    loop.call_soon_threadsafe(lambda: asyncio.ensure_future(client.close()))
            except RuntimeError:
                pass

    # --- GUI'ye bildirimler ---------------------------------------------------
    def _log(self, text: str):
        log.info(text)
        self.events.put(("log", text))

    def _set_state(self, state: str):
        self.state = state

    def _snapshot(self, dm_i: int, dm_total: int) -> dict:
        return {
            "dm_i": dm_i, "dm_total": dm_total, "deleted": self.deleted,
            "scanned": self.scanned, "failed": self.failed, "delay": self.cur_delay,
            "rl_hits": self.rl_hits, "state": self.state, "last": self.last_action,
        }

    def _progress(self, dm_i: int, dm_total: int):
        self.events.put(("progress", self._snapshot(dm_i, dm_total)))

    # --- İşlem özeti (hassas veri yok: token/mesaj içeriği/kullanıcı adı yazılmaz) ----
    def _write_summary(self, status: str):
        try:
            data = {
                "status": status,
                "mode": self.opt.get("mode"),
                "scanned": self.scanned, "deleted": self.deleted, "failed": self.failed,
                "rate_limit_hits": self.rl_hits,
                "completed_channel_ids": self._done_channels,
                "updated": datetime.now().isoformat(timespec="seconds"),
            }
            tmp = SUMMARY_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, SUMMARY_FILE)
        except OSError:
            log.exception("İşlem özeti yazılamadı")

    # --- Thread / döngü yönetimi ----------------------------------------------
    def _thread_main(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        try:
            loop.run_until_complete(self._run())
        except Exception as exc:  # noqa: BLE001
            log.exception("İşçi thread hatası")
            self.events.put(("log", f"Beklenmeyen hata: {exc!r} (ayrıntı: app.log)"))
        finally:
            try:
                pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.run_until_complete(asyncio.sleep(0.25))
            except Exception:  # noqa: BLE001
                log.exception("Döngü kapatılırken hata")
            finally:
                loop.close()
                if self._deleted_fh:
                    try:
                        self._deleted_fh.close()
                    except OSError:
                        pass
                self.events.put(("done", self.deleted, self.scanned, self.failed))

    async def _run(self):
        self._go = asyncio.Event()
        try:
            client = discord.Client(chunk_guilds_at_startup=False)
        except TypeError:
            client = discord.Client()
        self.client = client
        if self.stop_flag.is_set():     # pencere, işçi thread hazır olmadan kapatıldı: hiç bağlanma
            await client.close()
            return
        ready_seen = False

        @client.event
        async def on_ready():
            nonlocal ready_seen
            if ready_seen:  # yeniden bağlanmalarda tekrar tetiklenebilir
                return
            ready_seen = True
            try:
                await self._after_ready(client)
            except Exception as exc:  # noqa: BLE001
                log.exception("Silme işi hatası")
                self._log(f"Beklenmeyen hata: {exc!r} (ayrıntı: app.log)")
                self._write_summary("hata")
            finally:
                await client.close()

        self._log("Discord'a bağlanılıyor...")
        try:
            await client.start(self.token)
        except discord.LoginFailure:
            self.events.put(("auth_failed", "Token geçersiz veya süresi dolmuş (401). Token'ı kontrol et."))
        except discord.HTTPException as exc:
            if exc.status == 401:
                self.events.put(("auth_failed", "Token geçersiz veya süresi dolmuş (401). Token'ı kontrol et."))
            else:
                self.events.put(("auth_failed", f"Bağlantı hatası (HTTP {exc.status}): {exc.text or exc}"))
        except (OSError, asyncio.TimeoutError) as exc:
            self.events.put(("auth_failed", f"Ağ hatası: {exc!r}. İnternet bağlantını kontrol et."))
        finally:
            if not client.is_closed():
                await client.close()

    async def _sleep(self, seconds: float):
        """Durdur butonuna hızlı tepki veren bekleme."""
        loop = asyncio.get_running_loop()
        end = loop.time() + seconds
        while not self.stop_flag.is_set():
            remaining = end - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.2, remaining))

    # --- Aşama 1: doğrulama ---------------------------------------------------
    NET_RETRIES = 3

    @staticmethod
    def _fetch_avatar(url: str):
        """Avatar görselini CDN'den çek (token gönderilmez). Başarısızsa None."""
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310
                return r.read(2_000_000)
        except Exception:  # noqa: BLE001
            return None

    async def _avatar_bytes(self, user):
        """Avatarı, Discord'un kendi istemci verisindeki (display_avatar) CDN adresinden indirir.
        Üçüncü taraf servis kullanılmaz; token gönderilmez. Başarısızsa None (arayüz varsayılan görsel çizer)."""
        try:
            url = str(user.display_avatar.replace(size=128, format="png").url)
            return await asyncio.get_running_loop().run_in_executor(None, self._fetch_avatar, url)
        except Exception:  # noqa: BLE001
            return None

    async def _after_ready(self, client):
        me = client.user
        avatar_bytes = await self._avatar_bytes(me)
        info = {
            "username": str(getattr(me, "name", me)),
            "display_name": getattr(me, "global_name", None) or getattr(me, "display_name", None),
            "id": me.id,
            "avatar": avatar_bytes,
            "dm_total": sum(1 for c in client.private_channels if isinstance(c, discord.DMChannel)),
            "group_total": sum(1 for c in client.private_channels if isinstance(c, discord.GroupChannel)),
        }
        self._log(f"Bağlantı doğrulandı: {info['username']} (ID: {me.id})")
        self.events.put(("account", info))
        self._set_state("onay bekliyor")

        await self._go.wait()           # kullanıcı onaylayana kadar HİÇBİR şey silinmez
        if self.stop_flag.is_set():
            self._log("Bağlantı kapatıldı.")
            return
        self._job_started = True
        await self._job(client)

    def _compute_preview(self, opt: dict):
        asyncio.ensure_future(self._preview_async(opt))

    async def _preview_async(self, opt: dict):
        info = {"channels": None, "target": None, "target_display": None, "target_avatar": None}
        try:
            channels = self._select_channels(self.client, opt)
            info["channels"] = len(channels)
            if opt.get("mode") == "user" and channels:
                recipient = channels[0].recipient
                info["target"] = self._channel_name(channels[0])
                info["target_display"] = getattr(recipient, "global_name", None)
                info["target_avatar"] = await self._avatar_bytes(recipient)
        except Exception:  # noqa: BLE001
            log.exception("Kapsam tahmini hesaplanamadı")
        self.events.put(("preview", info))

    # --- Aşama 2: silme -------------------------------------------------------
    @staticmethod
    def _select_channels(client, opt: dict) -> list:
        mode = opt["mode"]
        target_id = opt.get("user_id")
        channels = []
        for channel in client.private_channels:
            if isinstance(channel, discord.DMChannel):
                if mode == "user":
                    recipient = channel.recipient
                    if recipient is None or recipient.id != target_id:
                        continue
                channels.append(channel)
            elif isinstance(channel, discord.GroupChannel):
                if mode != "user" and opt.get("include_groups"):
                    channels.append(channel)
        return channels

    @staticmethod
    def _channel_name(channel) -> str:
        if isinstance(channel, discord.DMChannel):
            return str(channel.recipient) if channel.recipient else f"DM {channel.id}"
        return f"Grup: {channel.name or channel.id}"

    async def _job(self, client):
        me = client.user
        self._started_at = time.monotonic()
        self._set_state("çalışıyor")
        self._log("İşlem başlatıldı.")
        self._write_summary("çalışıyor")
        status = "tamamlandı"
        try:
            self._log("Sohbet listesi yükleniyor.")
            channels = self._select_channels(client, self.opt)
            total = len(channels)
            if total == 0:
                self._log("Bu kapsamda işlenecek DM kanalı bulunamadı.")
                return

            after, before = self.opt.get("after"), self.opt.get("before")
            if after:
                self._log(f"Başlangıç: {after:%d.%m.%Y %H:%M} (UTC) ve sonrası taranacak.")
            if before:
                self._log(f"Bitiş: {before:%d.%m.%Y %H:%M} (UTC) ve öncesi taranacak.")

            self._log(f"{total} DM kanalı taranacak.")
            self._progress(0, total)

            for index, channel in enumerate(channels, start=1):
                if self.stop_flag.is_set():
                    break
                name = self._channel_name(channel)
                self._log(f"[{index}/{total}] Mesajlar işleniyor: {name}")
                try:
                    count = await self._purge_with_retry(channel, me.id, after, before, index, total)
                    self._log(f"[{index}/{total}] {name}: {count} mesaj silindi.")
                    if not self.stop_flag.is_set():
                        self._done_channels.append(channel.id)
                except NetworkDown:
                    self._log("İnternet bağlantısı kesildi; işlem güvenli şekilde durduruldu. Bağlantı gelince yeniden başlat.")
                    status = "hata"
                    break
                except discord.Forbidden:
                    self._log(f"[{index}/{total}] {name}: erişim yok (403), atlandı.")
                except discord.NotFound:
                    self._log(f"[{index}/{total}] {name}: kanal bulunamadı (404), atlandı.")
                except discord.HTTPException as exc:
                    self._log(f"[{index}/{total}] {name}: HTTP {exc.status} hatası, atlandı. ({exc.text or exc})")
                self._progress(index, total)
                self._write_summary("çalışıyor")

            if status != "hata" and self.stop_flag.is_set():
                status = "durduruldu"
                self._log("İşlem durduruldu.")
            self._log(f"Bitti. Taranan: {self.scanned}, silinen: {self.deleted}, başarısız: {self.failed}")
        except Exception:
            status = "hata"
            raise
        finally:
            self._set_state(status)
            self._write_summary(status)

    async def _purge_with_retry(self, channel, my_id, after, before, index, total) -> int:
        """Ağ kesintisinde SINIRLI sayıda (NET_RETRIES), artan bekleme ile yeniden dene; sonra pes et.
        Aynı kanalı yeniden taramak güvenlidir: silinmiş mesajlar zaten listede yoktur."""
        for attempt in range(1, self.NET_RETRIES + 1):
            try:
                return await self._purge_channel(channel, my_id, after, before, index, total)
            except (OSError, asyncio.TimeoutError):
                if attempt == self.NET_RETRIES or self.stop_flag.is_set():
                    raise NetworkDown() from None
                self._log(f"Geçici hata oluştu, yeniden deneniyor (ağ {attempt}/{self.NET_RETRIES}).")
                await self._sleep(5 * attempt)

    async def _purge_channel(self, channel, my_id: int, after, before, index: int, total: int) -> int:
        """Tek bir DM kanalında yalnızca bana ait ve kapsam içindeki mesajları sil."""
        deleted_here = 0
        seen = 0
        async for msg in channel.history(limit=None, after=after, before=before):
            if self.stop_flag.is_set():
                break
            seen += 1
            self.scanned += 1

            # GÜVENLİK KURALI 1: yalnızca benim yazdığım mesajlar. Başkasınınki asla.
            # GÜVENLİK KURALI 2: tarih sınırı dışındaki mesaj (API ne dönerse dönsün) asla.
            in_scope = (after is None or msg.created_at > after) and (before is None or msg.created_at < before)
            if msg.author.id == my_id and not msg.is_system() and in_scope:
                before_429 = RATE_WATCHER.count
                ok = await self._delete(msg, channel)
                hits = RATE_WATCHER.count - before_429
                self.rl_hits += max(0, hits)
                self._adapt_delay(hits)
                if ok:
                    deleted_here += 1
                    self.deleted += 1
                    if self.deleted % self.BATCH == 0:
                        self._progress(index - 1, total)
                delay = self.cur_delay
                await self._sleep(delay + random.uniform(0, delay * 0.3))

            if seen % self.BATCH == 0:
                self._progress(index - 1, total)
                await asyncio.sleep(0)
        return deleted_here

    def _adapt_delay(self, hits: int):
        """429 yediysek beklemeyi artır; uzun süre temiz gidersek azar azar (tabana kadar) düşür."""
        if hits > 0:
            self._clean_streak = 0
            old = self.cur_delay
            self.cur_delay = min(self.MAX_DELAY, max(self.cur_delay * 1.5, self.cur_delay + 0.5))
            if self.cur_delay - old >= 0.05:
                self._log(f"Rate limit nedeniyle bekleniyor: bekleme {old:.1f} -> {self.cur_delay:.1f} sn.")
        else:
            self._clean_streak += 1
            if self._clean_streak >= 20 and self.cur_delay > self.base_delay:
                self._clean_streak = 0
                self.cur_delay = max(self.base_delay, self.cur_delay * 0.9)

    @staticmethod
    def _retry_after(exc) -> float:
        headers = getattr(getattr(exc, "response", None), "headers", None) or {}
        for key in ("Retry-After", "X-RateLimit-Reset-After"):
            try:
                value = float(headers.get(key))
                if value > 0:
                    return value
            except (TypeError, ValueError):
                pass
        try:
            return float(getattr(exc, "retry_after", None) or 5.0)
        except (TypeError, ValueError):
            return 5.0

    async def _delete(self, msg, channel) -> bool:
        """Mesajı sil. 429'da sunucunun verdiği süre kadar bekleyip (sınırlı sayıda) tekrar dener."""
        for _attempt in range(6):
            if self.stop_flag.is_set():
                return False
            try:
                await msg.delete()
                self.last_action = f"{self._channel_name(channel)} · mesaj {msg.id} silindi"
                self._record_deleted(msg, channel)
                return True
            except discord.NotFound:
                return False  # zaten silinmiş
            except discord.Forbidden:
                self.failed += 1
                return False
            except discord.HTTPException as exc:
                if exc.status == 429:
                    wait = self._retry_after(exc) + 0.5
                    self.rl_hits += 1
                    self._adapt_delay(1)       # açık 429: taban beklemeyi de artır (sunucu süresi ayrıca uygulanır)
                    self._log(f"Rate limit nedeniyle bekleniyor (429): {wait:.1f} sn...")
                    await self._sleep(wait)
                    continue
                if exc.status >= 500:
                    self._log("Geçici hata oluştu, yeniden deneniyor.")
                    await self._sleep(3)
                    continue
                self.failed += 1
                self._log(f"Mesaj {msg.id} silinemedi (HTTP {exc.status}), atlandı.")
                return False
            except (OSError, asyncio.TimeoutError):
                self._log("Geçici hata oluştu, yeniden deneniyor.")
                await self._sleep(3)
        self.failed += 1
        self._log(f"Mesaj {msg.id} tekrar denemelere rağmen silinemedi, atlandı.")
        return False

    def _record_deleted(self, msg, channel):
        """İsteğe bağlı: silinen mesajı dosyaya kaydet (dosya tutamacı bir kez açılır)."""
        if not self.opt.get("save_deleted"):
            return
        try:
            if self._deleted_fh is None:
                self._deleted_fh = open(DELETED_LOG, "a", encoding="utf-8")
            content = (msg.content or "").replace("\r", " ").replace("\n", " ")
            if msg.attachments:
                content += f" [ek: {len(msg.attachments)}]"
            self._deleted_fh.write(
                f"{datetime.now():%Y-%m-%d %H:%M:%S}\t{self._channel_name(channel)}\t"
                f"{msg.id}\t{msg.created_at:%Y-%m-%d %H:%M:%S}\t{content}\n")
            self._deleted_fh.flush()
        except OSError:
            log.exception("Silinen mesaj kaydı yazılamadı")


# =============================================================================
# 2) ANİMASYON YARDIMCILARI
# =============================================================================
_now = time.monotonic   # testlerde sahte saatle değiştirilebilir


def clamp(x, lo=0.0, hi=1.0):
    return lo if x < lo else hi if x > hi else x


def ease_out_cubic(t: float) -> float:
    t = clamp(t)
    return 1 - (1 - t) ** 3


def ease_in_out(t: float) -> float:
    t = clamp(t)
    return 3 * t * t - 2 * t * t * t


def ease_out_back(t: float) -> float:
    """Hafif aşıp yerine oturan (pop) easing: logo girişi için."""
    t = clamp(t)
    c1, c3 = 1.70158, 2.70158
    return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2


def _hex_to_rgb(c: str):
    c = c.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def lerp_color(c1: str, c2: str, t: float) -> str:
    """İki #rrggbb rengi arasında doğrusal karışım (t: 0..1)."""
    t = clamp(t)
    a, b = _hex_to_rgb(c1), _hex_to_rgb(c2)
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def lighten(c: str, amount: float = 0.25) -> str:
    return lerp_color(c, "#ffffff", amount)


class Tween:
    """`step(eased_t)` fonksiyonunu `duration_ms` boyunca ~60 fps çağırır; sonunda tam 1.0 ile bir kez çağırıp `done()` çalıştırır."""

    def __init__(self, root, duration_ms, step, done=None, easing=ease_out_cubic, interval_ms=15):
        self.root = root
        self.duration = max(1, duration_ms) / 1000.0
        self.step = step
        self.done = done
        self.easing = easing
        self.interval = interval_ms
        self.cancelled = False
        self.finished = False
        self._t0 = None

    def start(self):
        self._t0 = _now()
        self._tick()
        return self

    def cancel(self):
        self.cancelled = True

    def _tick(self):
        if self.cancelled or self.finished:
            return
        p = clamp((_now() - self._t0) / self.duration)
        try:
            self.step(self.easing(p) if p < 1 else 1.0)
        except Exception:  # noqa: BLE001  (widget yok edilmiş olabilir; animasyon sessizce biter)
            self.cancelled = True
            return
        if p >= 1:
            self.finished = True
            if self.done:
                try:
                    self.done()
                except Exception:  # noqa: BLE001
                    pass
            return
        self.root.after(self.interval, self._tick)


# =============================================================================
# 3) ARKA PLAN GÖRSELİ
# =============================================================================
BG_IMAGE = "assets/background.jpg"


class Backdrop:
    """Arka plan görselini BİR KEZ yükler. Boyuta göre 'cover' kırpar (oran korunur), karartır ve
    yalnızca son kareyi önbellekte tutar. Yüklenemezse `ok` False olur ve arayüz koyu zeminle çalışır."""

    MAX_SIDE = 2200          # daha büyük kaynakları bir kez küçült (bellek/CPU)

    def __init__(self, path: str):
        self.src = None
        self.error = None
        self.animated = False
        self._cache = {}
        if Image is None:
            self.error = "Pillow yüklü değil"
            return
        try:
            im = Image.open(path)
            self.animated = bool(getattr(im, "is_animated", False)) and getattr(im, "n_frames", 1) > 1
            im.seek(0)                      # animasyonluysa yalnızca ilk kare; hareket varmış gibi gösterilmez
            im.load()
            self.src = im.convert("RGB")
            if max(self.src.size) > self.MAX_SIDE:
                self.src.thumbnail((self.MAX_SIDE, self.MAX_SIDE))
        except Exception as exc:  # noqa: BLE001  (dosya yok/bozuk/format desteklenmiyor)
            self.error = repr(exc)
            self.src = None
            log.warning("Arka plan görseli yüklenemedi, koyu zemin kullanılacak: %s", exc)

    @property
    def ok(self) -> bool:
        return self.src is not None

    def render(self, w: int, h: int, strength: float):
        """(w, h) boyutunda kırpılmış + karartılmış PIL görseli. `strength`: 0 = orijinal, 1 = tamamen koyu."""
        key = (int(w), int(h), round(strength, 2))
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        img = ImageOps.fit(self.src, (key[0], key[1]), method=Image.BILINEAR, centering=(0.5, 0.4))
        img = Image.blend(img, Image.new("RGB", img.size, (6, 7, 18)), strength)
        self._cache.clear()
        self._cache[key] = img
        return img


# =============================================================================
# 4) ARAYÜZ
# =============================================================================
BG = "#070813"          # siyah-lacivert taban
PANEL = "#0c0f22"       # cam panel (görselin üstünde)
SURFACE = "#141833"
SURFACE2 = "#1b2142"
BORDER = "#2b3160"
ACCENT = "#6d5efc"      # mor-mavi vurgu
ACCENT_HOVER = "#5848e6"
DANGER = "#da373c"
DANGER_HOVER = "#a12828"
SUCCESS = "#23a559"
WARN = "#f0b232"
TEXT = "#eef0ff"
MUTED = "#9aa0c8"

FONT = "Segoe UI"
MODE_LABELS = {
    "all": "Tüm DM'leri sil",
    "user": "Belirli kullanıcının DM'ini sil",
    "range": "Zamana göre sil",
}
MODE_ICONS = {"all": "", "user": "", "range": ""}   # Segoe Fluent/MDL2: Delete, Contact, Clock
STATE_COLORS = {
    "bağlanıyor": ACCENT, "onay bekliyor": WARN, "çalışıyor": ACCENT,
    "tamamlandı": SUCCESS, "durduruldu": WARN, "hata": DANGER,
}
SCREEN_ORDER = ["landing", "welcome", "home", "scope", "confirm", "run"]
SLIDE_MS = 260
INT_STATS = ("scanned", "deleted", "failed", "rl")
PANEL_MAX_W = 940
BG_STRENGTH = {"welcome": 0.50, "home": 0.50, "scope": 0.55, "confirm": 0.55, "run": 0.58}


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self._pick_fonts()
        self.title(APP_TITLE)
        self.geometry("1000x760")
        self.minsize(900, 700)
        self.configure(fg_color=BG)

        self.events: queue.Queue = queue.Queue()
        self.purger = None
        self.cfg = load_config()
        self.phase = "idle"          # idle | connecting | ready | running | finished
        self.account = None
        self.mode = "all"
        self.opt: dict = {}
        self.preview = None
        self._run_started = None
        self._avatar_img = None
        self._target_img = None
        self.backdrop = Backdrop(resource_path(BG_IMAGE))

        # Animasyon durumu
        self.anim_var = ctk.BooleanVar(value=bool(self.cfg.get("animations", True)))
        self.current = None
        self._trans = None
        self._stat_target = {k: 0 for k in INT_STATS}
        self._stat_shown = {k: 0 for k in INT_STATS}
        self._prog_target = 0.0
        self._prog_shown = 0.0
        self._pulse_t = 0.0
        self._dots_t = 0.0
        self._finish_tween = None
        self._glow_on = False
        self._closing = False

        self.container = ctk.CTkFrame(self, fg_color=BG)
        self.container.pack(fill="both", expand=True)

        self.screens = {}
        self.sc = {}      # ekran -> {cv, win, panel, photo, bg_size, job}
        self.screens["landing"] = self._build_landing_host()
        for name, builder in (("welcome", self._build_welcome), ("home", self._build_home),
                              ("scope", self._build_scope), ("confirm", self._build_confirm),
                              ("run", self._build_run)):
            self._make_screen(name, builder)

        self._load_saved()
        self._land_t0 = _now()
        self.show("landing", animate=False)
        self.bind("<Return>", lambda _e: self._land_go() if self.current == "landing" else None, add="+")
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll_events)
        self.after(500, self._tick)
        self.after(33, self._anim_loop)
        self._fade_in_window()
        self.after(33, self._land_loop)

        if discord is None:
            self._set_error(f"'discord' modülü yüklenemedi: {DISCORD_IMPORT_ERROR!r}. "
                            "Temiz bir venv içinde 'pip install -r requirements.txt' çalıştır.")
            self.connect_btn.configure(state="disabled")
        elif not hasattr(discord, "RelationshipType"):
            self._set_error("Normal 'discord.py' kurulu görünüyor; bu araç 'discord.py-self' ister. "
                            "Temiz venv -> pip uninstall discord.py -y && pip install discord.py-self")

    # --- Yazı tipleri, ikonlar ---------------------------------------------------
    def _pick_fonts(self):
        try:
            have = set(tkfont.families())
        except Exception:  # noqa: BLE001
            have = set()
        pick = lambda names, default=FONT: next((n for n in names if n in have), default)  # noqa: E731
        self.fam_head = pick(["Bahnschrift", "Segoe UI Variable Display", "Segoe UI Semibold", "Segoe UI"])
        self.fam_body = pick(["Segoe UI Variable Text", "Segoe UI"])
        self.fam_icon = pick(["Segoe Fluent Icons", "Segoe MDL2 Assets"], default=None)   # yalnızca Windows 10/11

    def F(self, size=13, bold=False):
        fam = self.fam_head if size >= 18 else self.fam_body
        return ctk.CTkFont(family=fam, size=size, weight="bold" if bold else "normal")

    def icon(self, parent, glyph, size=22, color=ACCENT):
        """İkon yazı tipi yoksa (Windows dışı) boş etiket döner; arayüz yine düzgün görünür."""
        if self.fam_icon is None:
            return ctk.CTkLabel(parent, text="", width=1, height=1)
        return ctk.CTkLabel(parent, text=glyph, text_color=color, font=ctk.CTkFont(family=self.fam_icon, size=size))

    # --- Animasyon çekirdeği ----------------------------------------------------
    @property
    def anim_on(self) -> bool:
        return bool(self.anim_var.get())

    def _hidden(self) -> bool:
        """Pencere küçültülmüşse animasyon döngüleri boşta bekler (CPU tasarrufu)."""
        try:
            return str(self.state()) == "iconic"
        except Exception:  # noqa: BLE001
            return False

    def _on_anim_toggle(self):
        self.cfg["animations"] = self.anim_on
        save_config(self.cfg)

    def _fade_in_window(self):
        if not self.anim_on:
            return
        try:
            self.attributes("-alpha", 0.0)
        except Exception:  # noqa: BLE001
            return
        Tween(self, 350, lambda t: self.attributes("-alpha", t), easing=ease_out_cubic).start()

    def _fade_in(self, groups, stagger_ms=110, dur_ms=480):
        """Her grup ((widget, özellik, son_renk) listesi) panel renginden belirerek sırayla görünür."""
        if not self.anim_on:
            return
        for i, group in enumerate(groups):
            def start(group=group):
                for w, prop, final in group:
                    def step(t, w=w, prop=prop, final=final):
                        w.configure(**{prop: lerp_color(PANEL, final, t)})
                    Tween(self, dur_ms, step, easing=ease_out_cubic).start()
            for w, prop, _final in group:
                try:
                    w.configure(**{prop: PANEL})
                except Exception:  # noqa: BLE001
                    pass
            self.after(40 + i * stagger_ms, start)

    def _place_screen(self, frame, relx=0.0):
        frame.place(relx=relx, rely=0, relwidth=1, relheight=1)

    def show(self, name, animate=True):
        """Ekranlar arası geçiş: yeni ekran yandan kayarak girer, eski ekran hafifçe geri çekilir."""
        new = self.screens[name]
        if self._trans is not None:
            self._trans.cancel()
            self._trans = None
            for n, s in self.screens.items():
                if n != self.current:
                    s.place_forget()
            self._place_screen(self.screens[self.current], 0.0)
        old_name = self.current
        if old_name == name:
            return
        self.current = name
        self._paint_bg(name)
        if old_name is None or not (animate and self.anim_on):
            for n, s in self.screens.items():
                if n != name:
                    s.place_forget()
            self._place_screen(new, 0.0)
            new.tkraise()
            return

        old = self.screens[old_name]
        direction = 1 if SCREEN_ORDER.index(name) >= SCREEN_ORDER.index(old_name) else -1
        self._place_screen(new, float(direction))
        new.tkraise()

        def step(t):
            self._place_screen(new, direction * (1 - t))
            self._place_screen(old, -direction * 0.22 * t)

        def done():
            self._trans = None
            old.place_forget()
            self._place_screen(new, 0.0)

        self._trans = Tween(self, SLIDE_MS, step, done, easing=ease_out_cubic).start()

    def _hover_tween(self, widget, base, hover):
        state = {"tween": None, "color": base}

        def go(target):
            if str(widget.cget("state")) == "disabled":
                target = base
            if state["tween"] is not None:
                state["tween"].cancel()
            if not self.anim_on:
                state["color"] = target
                widget.configure(fg_color=target)
                return
            start = state["color"]

            def step(t):
                c = lerp_color(start, target, t)
                state["color"] = c
                widget.configure(fg_color=c)
            state["tween"] = Tween(self, 140, step, easing=ease_in_out).start()

        widget.bind("<Enter>", lambda _e: go(hover), add="+")
        widget.bind("<Leave>", lambda _e: go(base), add="+")

    def _anim_loop(self):
        """~30 fps: nabız, akıcı ilerleme çubuğu, sayı sayma, 'Bağlanıyor...' noktaları."""
        if self._closing:
            return
        if self._hidden():
            self.after(250, self._anim_loop)
            return
        on = self.anim_on
        self._pulse_t += 0.033
        wave = (math.sin(self._pulse_t * 4.0) + 1) / 2

        diff = self._prog_target - self._prog_shown
        if abs(diff) > 0.0005:
            self._prog_shown = self._prog_target if not on else self._prog_shown + diff * 0.18
            self.progress.set(self._prog_shown)

        for k in INT_STATS:
            shown, target = self._stat_shown[k], self._stat_target[k]
            if shown != target:
                if on and target > shown:
                    shown = min(target, shown + max(1, int((target - shown) * 0.3)))
                else:
                    shown = target
                self._stat_shown[k] = shown
                self.stat[k].configure(text=str(shown))

        if on and self.phase == "running":
            self.run_pill.configure(fg_color=lerp_color(ACCENT, lighten(ACCENT, 0.35), wave))
        idle_glow = on and self.phase == "idle" and self.current == "welcome" and bool(discord)
        if idle_glow:
            self.connect_btn.configure(border_width=2, border_color=lerp_color(ACCENT, lighten(ACCENT, 0.65), wave))
            self._glow_on = True
        elif self._glow_on:
            self.connect_btn.configure(border_width=0)
            self._glow_on = False
        if self.phase == "connecting":
            if on:
                self.conn_pill.configure(fg_color=lerp_color(ACCENT, lighten(ACCENT, 0.35), wave))
            self._dots_t += 0.033
            self.connect_btn.configure(text="Bağlanıyor" + "." * (int(self._dots_t * 2.5) % 4))
        self.after(33, self._anim_loop)

    # --- Küçük arayüz yardımcıları ---------------------------------------------------
    def card(self, parent, **kw):
        return ctk.CTkFrame(parent, fg_color=SURFACE, corner_radius=12, border_width=1,
                            border_color=BORDER, **kw)

    def btn(self, parent, text, command, kind="primary", **kw):
        colors = {
            "primary": (ACCENT, ACCENT_HOVER, TEXT),
            "danger": (DANGER, DANGER_HOVER, TEXT),
            "ghost": (SURFACE2, BORDER, TEXT),
        }[kind]
        b = ctk.CTkButton(parent, text=text, command=command, fg_color=colors[0], hover=False,
                          text_color=colors[2], corner_radius=8, height=38, font=self.F(13, True), **kw)
        self._hover_tween(b, colors[0], colors[1])
        return b

    def label(self, parent, text, size=13, bold=False, color=TEXT, **kw):
        return ctk.CTkLabel(parent, text=text, font=self.F(size, bold), text_color=color, **kw)

    def pill(self, parent, text, color):
        return ctk.CTkLabel(parent, text=f"  ●  {text}  ", corner_radius=10, fg_color=color,
                            text_color="#ffffff", font=self.F(12, True), height=24)

    def entry(self, parent, **kw):
        return ctk.CTkEntry(parent, fg_color=PANEL, border_color=BORDER, text_color=TEXT,
                            height=38, corner_radius=8, font=self.F(13), **kw)

    def sep(self, parent, pady=(10, 10)):
        ctk.CTkFrame(parent, height=1, fg_color=BORDER).pack(fill="x", pady=pady)

    def _set_error(self, text):
        self.err_label.configure(text=text)

    def _avatar_image(self, data, size):
        """Doğrulanmış avatar baytlarından yuvarlak CTkImage; hata olursa None (harf avatarı kalır)."""
        if not data or Image is None:
            return None
        try:
            from PIL import ImageDraw
            img = Image.open(io.BytesIO(data)).convert("RGBA").resize((size, size))
            mask = Image.new("L", (size, size), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
            img.putalpha(mask)
            return ctk.CTkImage(light_image=img, dark_image=img, size=(size, size))
        except Exception:  # noqa: BLE001
            return None

    # --- Ekran iskeleti: canvas (arka plan görseli) + ortalanmış cam panel ----------------
    def _make_screen(self, name, builder):
        host = ctk.CTkFrame(self.container, fg_color=BG)
        cv = tk.Canvas(host, bg=BG, highlightthickness=0, bd=0)
        cv.pack(fill="both", expand=True)
        panel = ctk.CTkFrame(cv, fg_color=PANEL, corner_radius=0, border_width=1, border_color=BORDER)
        win = cv.create_window(0, 0, window=panel, anchor="center")
        self.sc[name] = {"cv": cv, "win": win, "panel": panel, "photo": None, "img_item": None,
                         "bg_size": None, "job": None}
        self.screens[name] = host
        cv.bind("<Configure>", lambda _e, n=name: self._screen_resized(n))
        builder(panel)

    def _screen_resized(self, name):
        s = self.sc[name]
        cv = s["cv"]
        w, h = cv.winfo_width(), cv.winfo_height()
        if w < 60 or h < 60:
            return
        cv.coords(s["win"], w / 2, h / 2)
        cv.itemconfigure(s["win"], width=min(w - 48, PANEL_MAX_W), height=max(300, h - 48))
        if s["job"] is not None:
            try:
                self.after_cancel(s["job"])
            except Exception:  # noqa: BLE001
                pass
        s["job"] = self.after(90, lambda: self._paint_bg(name))   # boyutlandırma sırasında art arda çizme

    def _paint_bg(self, name):
        """Arka plan görselini yalnızca görünür ekran için ve boyut değiştiyse yeniden üret."""
        if name == "landing" or name != self.current:
            return
        s = self.sc[name]
        cv = s["cv"]
        w, h = cv.winfo_width(), cv.winfo_height()
        if w < 60 or h < 60 or s["bg_size"] == (w, h):
            return
        if not (self.backdrop.ok and ImageTk is not None):
            return                                               # görsel yok -> koyu zemin
        try:
            photo = ImageTk.PhotoImage(self.backdrop.render(w, h, BG_STRENGTH.get(name, 0.5)))
        except Exception:  # noqa: BLE001
            log.exception("Arka plan çizilemedi")
            return
        s["photo"] = photo                                       # referansı tut (GC görseli silmesin)
        s["bg_size"] = (w, h)
        if s["img_item"] is None:
            s["img_item"] = cv.create_image(0, 0, anchor="nw", image=photo)
        else:
            cv.itemconfigure(s["img_item"], image=photo)
        cv.tag_lower(s["img_item"])

    # =========================================================================
    # 0) Animasyonlu açılış sayfası
    # =========================================================================
    LAND_TITLE = APP_TITLE
    LAND_SUB = "Kendi DM mesajlarını hızlı, güvenli ve kontrollü şekilde temizle."
    LAND_CHIPS = "Yalnızca kendi mesajların   •   Rate limit'e uyumlu   •   Onay almadan silmez"
    LAND_TOP = "#0a0b1c"
    LAND_VIOLET = "#9b59f5"

    @staticmethod
    def _rr(x1, y1, x2, y2, r):
        return [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2,
                x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]

    def _build_landing_host(self):
        f = ctk.CTkFrame(self.container, fg_color=BG)
        self.land = tk.Canvas(f, bg=BG, highlightthickness=0, bd=0)
        self.land.pack(fill="both", expand=True)
        self._land_size = (0, 0)
        self._land_hover = False
        self._land_hover_k = 0.0
        self._land_err = False
        self._land_photo = None
        self._land_font_title = tkfont.Font(family=self.fam_head, size=46, weight="bold")
        rng = random.Random(7)
        self._land_particles = [
            {"x": rng.random(), "y": rng.random(), "r": rng.uniform(1.3, 3.2), "v": rng.uniform(0.008, 0.030),
             "ph": rng.uniform(0, 6.28), "col": rng.choice([ACCENT, self.LAND_VIOLET, "#5ac8fa", "#ffffff"]),
             "id": None} for _ in range(16)]                      # düşük yoğunluk: yalnızca dekoratif
        self.land.bind("<Configure>", lambda _e: self._land_layout())
        return f

    def _land_layout(self):
        c = self.land
        w, h = c.winfo_width(), c.winfo_height()
        if w < 60 or h < 60:
            return
        c.delete("all")
        self._orbs = []
        if self.backdrop.ok and ImageTk is not None:
            try:
                self._land_photo = ImageTk.PhotoImage(self.backdrop.render(w, h, 0.30))
                c.create_image(0, 0, anchor="nw", image=self._land_photo)
            except Exception:  # noqa: BLE001
                log.exception("Açılış arka planı çizilemedi")
                self._land_photo = None
        if self._land_photo is None or not (self.backdrop.ok and ImageTk is not None):
            bands = 40
            for i in range(bands):
                c.create_rectangle(0, h * i / bands, w, h * (i + 1) / bands + 1, outline="",
                                   fill=lerp_color(self.LAND_TOP, BG, i / (bands - 1)))
            for col in (ACCENT, self.LAND_VIOLET):
                self._orbs.append([c.create_oval(0, 0, 1, 1, outline="",
                                                 fill=lerp_color(self.LAND_TOP, col, 0.03 + 0.20 * k / 8))
                                   for k in range(9)])
        for p in self._land_particles:
            p["id"] = c.create_oval(0, 0, 1, 1, fill=BG, outline="")
        self._l_rings = [c.create_oval(0, 0, 1, 1, outline=BG, width=2, fill="") for _ in range(2)]
        self._l_box = c.create_polygon(0, 0, 0, 0, 0, 0, fill=ACCENT, outline="", smooth=True)
        self._l_bubble = c.create_polygon(0, 0, 0, 0, 0, 0, fill="#ffffff", outline="", smooth=True)
        self._l_tail = c.create_polygon(0, 0, 0, 0, 0, 0, fill="#ffffff", outline="")
        self._l_dots = [c.create_oval(0, 0, 1, 1, fill="#c9cdfb", outline="") for _ in range(3)]
        self._l_title_sh = c.create_text(0, 0, text="", anchor="w", fill="#05060f", font=self._land_font_title)
        self._l_title = c.create_text(0, 0, text="", anchor="w", fill=TEXT, font=self._land_font_title)
        self._l_credit_sh = c.create_text(0, 0, text=CREDIT, fill="#05060f", font=self.F(13))
        self._l_credit = c.create_text(0, 0, text=CREDIT, fill=BG, font=self.F(13))
        self._l_sub = c.create_text(0, 0, text=self.LAND_SUB, fill=BG, font=self.F(14))
        self._l_chips = c.create_text(0, 0, text=self.LAND_CHIPS, fill=BG, font=self.F(12))
        self._l_glow = c.create_polygon(0, 0, 0, 0, 0, 0, fill="", outline=BG, width=2, smooth=True)
        self._l_btn = c.create_polygon(0, 0, 0, 0, 0, 0, fill=BG, outline="", smooth=True, tags="btn")
        self._l_btn_txt = c.create_text(0, 0, text="Başla  →", fill=BG, font=self.F(15, True), tags="btn")
        self._l_foot = c.create_text(0, 0, text="Self-token kullanımı Discord ToS'una aykırıdır; risk sana aittir.",
                                     fill=BG, font=self.F(10))
        c.tag_bind("btn", "<Button-1>", lambda _e: self._land_go())
        c.tag_bind("btn", "<Enter>", lambda _e: self._land_set_hover(True))
        c.tag_bind("btn", "<Leave>", lambda _e: self._land_set_hover(False))
        self._land_size = (w, h)

    def _land_set_hover(self, state):
        self._land_hover = state
        self.land.configure(cursor="hand2" if state else "")

    def _land_go(self):
        if self.current == "landing":
            self.show("welcome")
            self._welcome_intro()

    def _land_loop(self):
        if self._closing:
            return
        if self._hidden():
            self.after(250, self._land_loop)
            return
        try:
            if self.current == "landing":
                self._land_frame()
        except Exception:  # noqa: BLE001
            if not self._land_err:
                self._land_err = True
                log.exception("Açılış animasyonu hatası")
        self.after(33, self._land_loop)

    def _land_frame(self):
        c = self.land
        w, h = c.winfo_width(), c.winfo_height()
        if (w, h) != self._land_size:
            self._land_layout()
            w, h = self._land_size
            if not w:
                return
        on = self.anim_on
        t = (_now() - self._land_t0) if on else 99.0
        mt = (_now() - self._land_t0) if on else 0.0
        cx, wave = w / 2, (math.sin(mt * 3.2) + 1) / 2

        def fade(t0, dur=0.6):
            return ease_out_cubic(clamp((t - t0) / dur))

        for ids, (ax, ay, bx, by, R) in zip(self._orbs, (
                (0.22, 0.34, 0.08, 0.06, min(w, h) * 0.50), (0.80, 0.64, 0.07, 0.07, min(w, h) * 0.58))):
            ox = w * (ax + bx * math.sin(mt * 0.45 + ax * 9))
            oy = h * (ay + by * math.cos(mt * 0.38 + ay * 7))
            for k, oid in enumerate(ids):
                r = R * (1 - k * 0.105)
                c.coords(oid, ox - r, oy - r, ox + r, oy + r)
        for p in self._land_particles:
            px = w * (p["x"] + 0.02 * math.sin(mt * 0.6 + p["ph"]))
            py = h * ((p["y"] - mt * p["v"]) % 1.0)
            tw = (math.sin(mt * 1.6 + p["ph"]) + 1) / 2
            r = p["r"] * (0.8 + 0.5 * tw)
            c.coords(p["id"], px - r, py - r, px + r, py + r)
            c.itemconfigure(p["id"], fill=lerp_color(BG, p["col"], (0.20 + 0.55 * tw) * fade(0.2, 1.0)))

        ly = h * 0.23
        s = max(0.001, ease_out_back(clamp(t / 0.7)))
        S = 42 * s
        for i, rid in enumerate(self._l_rings):
            rr = (S + 14 + i * 16 + 7 * wave * (i + 1))
            c.coords(rid, cx - rr, ly - rr, cx + rr, ly + rr)
            c.itemconfigure(rid, outline=lerp_color(self.LAND_TOP, ACCENT, (0.34 - 0.14 * i) * (0.4 + 0.6 * wave) * clamp(t / 0.7)))
        c.coords(self._l_box, *self._rr(cx - S, ly - S, cx + S, ly + S, S * 0.42))
        bw, bh = S * 0.62, S * 0.42
        c.coords(self._l_bubble, *self._rr(cx - bw, ly - bh - S * 0.08, cx + bw, ly + bh - S * 0.08, bh * 0.7))
        c.coords(self._l_tail, cx - bw * 0.55, ly + bh - S * 0.10, cx - bw * 0.95, ly + bh + S * 0.30,
                 cx - bw * 0.05, ly + bh - S * 0.10)
        for i, did in enumerate(self._l_dots):
            lvl = max(0.0, math.sin(mt * 5.0 - i * 0.9))
            dr = S * (0.085 + 0.035 * lvl)
            dx, dy = cx + (i - 1) * S * 0.32, ly - S * 0.08 - S * 0.07 * lvl
            c.coords(did, dx - dr, dy - dr, dx + dr, dy + dr)
            c.itemconfigure(did, fill=lerp_color("#c9cdfb", ACCENT_HOVER, lvl))

        title = self.LAND_TITLE
        n = int(clamp((t - 0.7) * 14, 0, len(title)))
        cursor = "▏" if (n < len(title) or (int(t * 2) % 2 == 0 and t < 5.0)) else ""
        x0 = cx - self._land_font_title.measure(title) / 2
        ty = h * 0.44
        c.coords(self._l_title, x0, ty)
        c.coords(self._l_title_sh, x0 + 2, ty + 2)
        c.itemconfigure(self._l_title, text=title[:n] + cursor)
        c.itemconfigure(self._l_title_sh, text=title[:n])
        k = fade(1.9)
        c.coords(self._l_credit, cx, h * 0.52 + 10 * (1 - k))
        c.coords(self._l_credit_sh, cx + 1, h * 0.52 + 10 * (1 - k) + 1)
        c.itemconfigure(self._l_credit, fill=lerp_color(BG, "#c9cdfb", k))
        c.itemconfigure(self._l_credit_sh, fill=lerp_color(BG, "#05060f", k))
        k = fade(2.2)
        c.coords(self._l_sub, cx, h * 0.60 + 12 * (1 - k))
        c.itemconfigure(self._l_sub, fill=lerp_color(BG, "#b7bcdf", k))
        k = fade(2.5)
        c.coords(self._l_chips, cx, h * 0.655 + 12 * (1 - k))
        c.itemconfigure(self._l_chips, fill=lerp_color(BG, "#9aa0c8", k))

        kb = fade(2.8, 0.7)
        self._land_hover_k += ((1.0 if self._land_hover else 0.0) - self._land_hover_k) * 0.25
        hk = self._land_hover_k
        by = h * 0.775 + 14 * (1 - kb)
        bw2, bh2 = 120 + 8 * hk, 25 + 2 * hk
        c.coords(self._l_btn, *self._rr(cx - bw2, by - bh2, cx + bw2, by + bh2, bh2))
        c.itemconfigure(self._l_btn, fill=lerp_color(BG, lerp_color(ACCENT, ACCENT_HOVER, hk), kb))
        c.coords(self._l_btn_txt, cx, by)
        c.itemconfigure(self._l_btn_txt, fill=lerp_color(BG, "#ffffff", kb))
        g = 8 + 10 * wave
        c.coords(self._l_glow, *self._rr(cx - bw2 - g, by - bh2 - g, cx + bw2 + g, by + bh2 + g, bh2 + g))
        c.itemconfigure(self._l_glow, outline=lerp_color(self.LAND_TOP, ACCENT, 0.55 * (1 - wave * 0.6) * kb))
        k = fade(3.2)
        c.coords(self._l_foot, cx, h - 22)
        c.itemconfigure(self._l_foot, fill=lerp_color(BG, "#8c91b8", k))

    # =========================================================================
    # 1) Bağlantı ekranı
    # =========================================================================
    def _build_welcome(self, f):
        wrap = ctk.CTkFrame(f, fg_color="transparent")
        wrap.pack(fill="both", expand=True, padx=40, pady=26)

        head = ctk.CTkFrame(wrap, fg_color="transparent")
        head.pack(fill="x")
        self._w_icon = self.icon(head, "", 26)           # kilit
        self._w_icon.pack(side="left", padx=(0, 10))
        self._w_title = self.label(head, APP_TITLE, 30, True)
        self._w_title.pack(side="left")
        self._w_credit = self.label(wrap, CREDIT, 12, color=MUTED)
        self._w_credit.pack(anchor="w", pady=(0, 2))
        self._w_sub = self.label(wrap, "Kendi gönderdiğin DM mesajlarını toplu ve kontrollü şekilde sil. "
                                       "Karşı tarafın mesajlarına dokunulmaz.", 13, color=MUTED,
                                 wraplength=800, justify="left")
        self._w_sub.pack(anchor="w", pady=(2, 16))

        card = self.card(wrap)
        card.pack(fill="x")
        self._w_card = card
        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill="x", padx=18, pady=(16, 4))
        self.label(top, "Hesap bağlantısı", 15, True).pack(side="left")
        self.conn_pill = self.pill(top, "Bağlı değil", MUTED)
        self.conn_pill.pack(side="right")

        self.token_entry = self.entry(card, show="•", placeholder_text="Kullanıcı token'ı")
        self.token_entry.pack(fill="x", padx=18, pady=(8, 6))
        self.token_entry.bind("<Return>", lambda _e: self._on_connect())

        opts = ctk.CTkFrame(card, fg_color="transparent")
        opts.pack(fill="x", padx=18, pady=(0, 6))
        self.show_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(opts, text="Göster", variable=self.show_var, command=self._toggle_show,
                        fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=TEXT,
                        font=self.F(12)).pack(side="left")
        self.remember_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(opts, text="Token'ı hatırla (bu bilgisayarda şifreli saklanır)",
                        variable=self.remember_var, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                        text_color=TEXT, font=self.F(12)).pack(side="left", padx=(20, 0))

        self.err_label = self.label(card, "", 12, color="#ff8a8e", wraplength=780, justify="left")
        self.err_label.pack(anchor="w", padx=18)
        self.connect_btn = self.btn(card, "Bağlan ve hesabı doğrula", self._on_connect)
        self.connect_btn.pack(fill="x", padx=18, pady=(8, 18))

        ctk.CTkCheckBox(wrap, text="Animasyonlar", variable=self.anim_var, command=self._on_anim_toggle,
                        fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=MUTED,
                        font=self.F(11)).pack(side="bottom", anchor="e", pady=(10, 0))

        warn = ctk.CTkFrame(wrap, fg_color="#2c2515", corner_radius=12, border_width=1, border_color="#6b5420")
        warn.pack(fill="x", pady=(16, 0))
        self._w_warn = warn
        top = ctk.CTkFrame(warn, fg_color="transparent")
        top.pack(fill="x", padx=16, pady=(12, 2))
        self.icon(top, "", 16, WARN).pack(side="left", padx=(0, 8))
        self._w_warn_t1 = self.label(top, "Güvenlik ve hesap uyarısı", 13, True, color=WARN)
        self._w_warn_t1.pack(side="left")
        self._w_warn_t2 = self.label(
            warn,
            "• Self-token ile otomasyon Discord Hizmet Şartları'na aykırıdır; hesabın kısıtlanabilir veya "
            "kapatılabilir. Riski sen üstlenirsin.\n"
            "• Token hesabının tam yetkisidir; kimseyle paylaşma. Token yalnızca Discord'a gönderilir, "
            "log'lara yazılmaz.\n"
            "• Silinen mesajlar geri getirilemez.",
            12, color=TEXT, wraplength=800, justify="left")
        self._w_warn_t2.pack(anchor="w", padx=16, pady=(0, 12))

    def _toggle_show(self):
        self.token_entry.configure(show="" if self.show_var.get() else "•")

    def _load_saved(self):
        stored = self.cfg.get("token")
        if stored:
            token = unprotect_token(stored)
            if token:
                self.token_entry.insert(0, token)
                self.remember_var.set(True)

    def _welcome_intro(self):
        self._fade_in([
            [(self._w_title, "text_color", TEXT)],
            [(self._w_credit, "text_color", MUTED), (self._w_sub, "text_color", MUTED)],
            [(self._w_card, "fg_color", SURFACE), (self._w_card, "border_color", BORDER)],
            [(self._w_warn, "fg_color", "#2c2515"), (self._w_warn, "border_color", "#6b5420"),
             (self._w_warn_t1, "text_color", WARN), (self._w_warn_t2, "text_color", TEXT)],
        ])

    def _on_connect(self):
        if self.purger is not None or self.phase != "idle":
            return
        token = self.token_entry.get().strip().strip('"').strip("'")
        if not token:
            self._set_error("Token boş olamaz.")
            return
        if self.remember_var.get():
            self.cfg["token"] = protect_token(token)
        else:
            self.cfg.pop("token", None)
        save_config(self.cfg)

        self._set_error("")
        self.connect_btn.configure(state="disabled", text="Bağlanıyor...")
        self.conn_pill.configure(text="  ●  Bağlanıyor  ", fg_color=ACCENT)
        self.phase = "connecting"
        self.purger = Purger(token, self._preset_delay(DEFAULT_SPEED), self.events)
        self.purger.start()

    def _reset_welcome(self, message=""):
        self.phase = "idle"
        self.purger = None
        self.account = None
        self.connect_btn.configure(state="normal" if discord else "disabled", text="Bağlan ve hesabı doğrula")
        self.conn_pill.configure(text="  ●  Bağlı değil  ", fg_color=MUTED)
        self._set_error(message)
        self.show("welcome")
        self._welcome_intro()

    # =========================================================================
    # 2) Ana ekran: profil kartı + işlem seçimi
    # =========================================================================
    def _build_home(self, f):
        wrap = ctk.CTkFrame(f, fg_color="transparent")
        wrap.pack(fill="both", expand=True, padx=40, pady=26)

        acc = self.card(wrap)
        acc.pack(fill="x")
        self._h_acc = acc
        row = ctk.CTkFrame(acc, fg_color="transparent")
        row.pack(fill="x", padx=18, pady=16)
        self.avatar_lbl = ctk.CTkLabel(row, text="?", width=72, height=72, corner_radius=36,
                                       fg_color=ACCENT, font=self.F(28, True), text_color="#fff")
        self.avatar_lbl.pack(side="left")
        info = ctk.CTkFrame(row, fg_color="transparent")
        info.pack(side="left", padx=16)
        self.acc_name = self.label(info, "", 20, True)
        self.acc_name.pack(anchor="w")
        self.acc_user = self.label(info, "", 13, color=MUTED)
        self.acc_user.pack(anchor="w")
        self.acc_id = self.label(info, "", 12, color=MUTED)
        self.acc_id.pack(anchor="w")
        right = ctk.CTkFrame(row, fg_color="transparent")
        right.pack(side="right")
        self.pill(right, "Bağlı", SUCCESS).pack(anchor="e")
        self.acc_dms = self.label(right, "", 12, color=MUTED)
        self.acc_dms.pack(anchor="e", pady=(6, 0))

        self._h_head = self.label(wrap, "Ne yapmak istiyorsun?", 18, True)
        self._h_head.pack(anchor="w", pady=(22, 8))
        self._h_cards = []
        cards = ctk.CTkFrame(wrap, fg_color="transparent")
        cards.pack(fill="x")
        for i in range(3):
            cards.grid_columnconfigure(i, weight=1, uniform="a")
        items = (
            ("all", "Tüm DM'ler", "Açık tüm DM sohbetlerindeki kendi mesajların silinir."),
            ("user", "Kullanıcı ID'sine göre", "Yalnızca girdiğin kullanıcı ID'siyle olan DM'deki mesajların silinir."),
            ("range", "Zamana göre", "Seçtiğin tarih ve saat aralığındaki mesajların silinir."),
        )
        for i, (mode, title, desc) in enumerate(items):
            c = self.card(cards)
            c.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 8, 0 if i == 2 else 8))
            self._h_cards.append(c)
            self.icon(c, MODE_ICONS[mode], 26).pack(anchor="w", padx=16, pady=(16, 2))
            self.label(c, title, 15, True).pack(anchor="w", padx=16, pady=(0, 4))
            self.label(c, desc, 12, color=MUTED, wraplength=220, justify="left").pack(
                anchor="w", padx=16, pady=(0, 12))
            self.btn(c, "Seç", lambda m=mode: self._choose_mode(m)).pack(fill="x", padx=16, pady=(0, 16))

        self.btn(wrap, "Bağlantıyı kes", self._on_disconnect, "ghost", width=160).pack(anchor="w", pady=(22, 0))

    def _home_intro(self):
        groups = [[(self._h_acc, "fg_color", SURFACE), (self._h_acc, "border_color", BORDER)],
                  [(self._h_head, "text_color", TEXT)]]
        for c in self._h_cards:
            groups.append([(c, "fg_color", SURFACE), (c, "border_color", BORDER)])
        self._fade_in(groups, stagger_ms=90)

    def _show_account(self, info):
        self.account = info
        name = info["display_name"] or info["username"]
        self.acc_name.configure(text=name)
        self.acc_user.configure(text=f"@{info['username']}")
        self.acc_id.configure(text=f"ID: {info['id']}")
        self.acc_dms.configure(text=f"{info['dm_total']} DM · {info['group_total']} grup")
        self.avatar_lbl.configure(text=(name[:1] or "?").upper(), image=None)
        self._avatar_img = self._avatar_image(info.get("avatar"), 72)     # Discord CDN'den; yoksa harf avatarı
        if self._avatar_img is not None:
            self.avatar_lbl.configure(image=self._avatar_img, text="")

    def _on_disconnect(self):
        if self.purger is not None:
            self.purger.stop()

    def _choose_mode(self, mode):
        self.mode = mode
        self.scope_title.configure(text=MODE_LABELS[mode])
        self.scope_err.configure(text="")
        for w in (self.user_frame, self.range_frame, self.all_frame):
            w.pack_forget()
        if mode == "user":
            self.user_frame.pack(fill="x", padx=18, pady=(0, 8))
            self.groups_cb.configure(state="disabled")
        elif mode == "range":
            self.range_frame.pack(fill="x", padx=18, pady=(0, 8))
            self.groups_cb.configure(state="normal")
        else:
            self.all_frame.pack(fill="x", padx=18, pady=(0, 8))
            self.groups_cb.configure(state="normal")
        self.show("scope")

    # =========================================================================
    # 3) Kapsam + hız ekranı
    # =========================================================================
    def _preset_delay(self, name):
        for n, d, _desc in SPEED_PRESETS:
            if n == name and d is not None:
                return d
        return 1.5

    def _build_scope(self, f):
        nav = ctk.CTkFrame(f, fg_color="transparent")
        nav.pack(side="bottom", fill="x", padx=40, pady=(8, 22))
        self.btn(nav, "← Geri", lambda: self.show("home"), "ghost", width=120).pack(side="left")
        self.btn(nav, "Devam →", self._scope_next, width=160).pack(side="right")
        self.sep(f, pady=(0, 0))    # ince ayraç: kaydırılan içerik ile gezinti çubuğu arasında

        wrap = ctk.CTkScrollableFrame(f, fg_color="transparent")    # içerik sığmazsa kaydırılır
        wrap.pack(fill="both", expand=True, padx=28, pady=(18, 0))
        head = ctk.CTkFrame(wrap, fg_color="transparent")
        head.pack(fill="x", pady=(0, 12), padx=6)
        self.scope_icon = self.icon(head, "", 24)
        self.scope_icon.pack(side="left", padx=(0, 10))
        self.scope_title = self.label(head, "", 22, True)
        self.scope_title.pack(side="left")

        card = self.card(wrap)
        card.pack(fill="x", padx=6)
        self.label(card, "Kapsam", 15, True).pack(anchor="w", padx=18, pady=(16, 8))

        self.all_frame = ctk.CTkFrame(card, fg_color="transparent")
        self.label(self.all_frame, "Hesabında açık olan tüm DM sohbetleri taranır. Yalnızca senin "
                   "yazdığın mesajlar silinir.", 12, color=MUTED, wraplength=760,
                   justify="left").pack(anchor="w")

        self.user_frame = ctk.CTkFrame(card, fg_color="transparent")
        self.label(self.user_frame, "Hedef kullanıcının Discord ID'si (Geliştirici Modu → Kullanıcı ID'sini kopyala)",
                   12, color=MUTED).pack(anchor="w", pady=(0, 6))
        self.user_entry = self.entry(self.user_frame, placeholder_text="örn. 123456789012345678")
        self.user_entry.pack(fill="x")

        self.range_frame = ctk.CTkFrame(card, fg_color="transparent")
        self.label(self.range_frame, f"Tarih biçimi GG.AA.YYYY, saat SS:DD · saat dilimi: {local_tz_label()} "
                   "(bilgisayarının yerel saati). Saat boşsa başlangıç 00:00, bitiş 23:59 alınır. "
                   "Tek bir sınır da girebilirsin.", 12, color=MUTED, wraplength=760,
                   justify="left").pack(anchor="w", pady=(0, 8))
        grid = ctk.CTkFrame(self.range_frame, fg_color="transparent")
        grid.pack(fill="x")
        self.label(grid, "Başlangıç", 12, color=MUTED).grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.start_date = self.entry(grid, width=150, placeholder_text="GG.AA.YYYY")
        self.start_date.grid(row=0, column=1, padx=(0, 8), pady=4)
        self.start_time = self.entry(grid, width=90, placeholder_text="SS:DD")
        self.start_time.grid(row=0, column=2, pady=4)
        self.label(grid, "Bitiş", 12, color=MUTED).grid(row=1, column=0, sticky="w", padx=(0, 10))
        self.end_date = self.entry(grid, width=150, placeholder_text="GG.AA.YYYY")
        self.end_date.grid(row=1, column=1, padx=(0, 8), pady=4)
        self.end_time = self.entry(grid, width=90, placeholder_text="SS:DD")
        self.end_time.grid(row=1, column=2, pady=4)
        self.btn(grid, "Aralığı temizle", self._clear_range, "ghost", width=140).grid(
            row=0, column=3, rowspan=2, padx=(16, 0))

        self.label(card, "Seçenekler", 15, True).pack(anchor="w", padx=18, pady=(10, 6))
        self.groups_var = ctk.BooleanVar(value=False)
        self.groups_cb = ctk.CTkCheckBox(card, text="Grup DM'lerini de dahil et", variable=self.groups_var,
                                         fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=TEXT,
                                         font=self.F(12))
        self.groups_cb.pack(anchor="w", padx=18, pady=3)
        self.save_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(card, text="Silinen mesajları dosyaya kaydet (silinmeden önce içerik yedeklenir)",
                        variable=self.save_var, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                        text_color=TEXT, font=self.F(12)).pack(anchor="w", padx=18, pady=(3, 12))

        # --- Silme hızı: 5 seçenek ---
        speed = self.card(wrap)
        speed.pack(fill="x", padx=6, pady=(12, 8))
        top = ctk.CTkFrame(speed, fg_color="transparent")
        top.pack(fill="x", padx=18, pady=(16, 8))
        self.label(top, "Silme hızı", 15, True).pack(side="left")
        self.speed_val = self.label(top, "", 12, color=MUTED)
        self.speed_val.pack(side="right")
        self.speed_seg = ctk.CTkSegmentedButton(
            speed, values=[n for n, _d, _t in SPEED_PRESETS], command=self._on_speed,
            fg_color=SURFACE2, selected_color=ACCENT, selected_hover_color=ACCENT_HOVER,
            unselected_color=SURFACE2, unselected_hover_color=BORDER, text_color=TEXT, font=self.F(12, True))
        self.speed_seg.pack(fill="x", padx=18)
        self.speed_desc = self.label(speed, "", 12, color=MUTED, wraplength=760, justify="left")
        self.speed_desc.pack(anchor="w", padx=18, pady=(8, 4))
        self.custom_row = ctk.CTkFrame(speed, fg_color="transparent")
        self.label(self.custom_row, "Bekleme (sn):", 12, color=MUTED).pack(side="left")
        self.custom_entry = self.entry(self.custom_row, width=90)
        self.custom_entry.insert(0, "1.5")
        self.custom_entry.pack(side="left", padx=10)
        self.label(speed, "Bu değerler yalnızca silmeler arası TABAN beklemedir. Discord 429 verirse "
                   "bekleme otomatik artar ve sunucunun Retry-After süresi her zaman uygulanır. "
                   "Hiçbir seçenek 429 almayacağını garanti etmez.", 11, color=MUTED, wraplength=760,
                   justify="left").pack(anchor="w", padx=18, pady=(2, 14))
        self.speed_seg.set(DEFAULT_SPEED)
        self._on_speed(DEFAULT_SPEED)

        self.scope_err = self.label(wrap, "", 12, color="#ff8a8e", wraplength=760, justify="left")
        self.scope_err.pack(anchor="w", padx=10, pady=(0, 6))

    def _on_speed(self, name):
        for n, d, desc in SPEED_PRESETS:
            if n == name:
                self.speed_desc.configure(text=desc)
                self.speed_val.configure(text=f"{d:.1f} sn" if d is not None else "özel")
                break
        if name == "Özel":
            self.custom_row.pack(anchor="w", padx=18, pady=(0, 4))
        else:
            self.custom_row.pack_forget()

    def _selected_delay(self):
        """(ad, saniye) döner; geçersiz özel değerde ValueError."""
        name = self.speed_seg.get() or DEFAULT_SPEED
        if name == "Özel":
            try:
                value = float(self.custom_entry.get().replace(",", "."))
            except ValueError:
                raise ValueError("Özel bekleme süresi sayı olmalı (örn. 1.5).") from None
            if value < MIN_DELAY:
                raise ValueError(f"Özel bekleme en az {MIN_DELAY} sn olmalı.")
            return name, value
        return name, self._preset_delay(name)

    def _clear_range(self):
        for e in (self.start_date, self.start_time, self.end_date, self.end_time):
            e.delete(0, "end")
        self.scope_err.configure(text="")

    def _scope_next(self):
        opt = {"mode": self.mode, "include_groups": bool(self.groups_var.get()) and self.mode != "user",
               "save_deleted": bool(self.save_var.get())}
        try:
            opt["speed"], opt["delay"] = self._selected_delay()
            if self.mode == "user":
                opt["user_id"] = validate_user_id(self.user_entry.get())
            elif self.mode == "range":
                opt["after"], opt["before"] = parse_range(
                    self.start_date.get(), self.start_time.get(), self.end_date.get(), self.end_time.get())
        except ValueError as exc:
            self.scope_err.configure(text=str(exc))
            return
        self.scope_err.configure(text="")
        self.opt = opt
        self.preview = None
        self._fill_confirm()
        self.show("confirm")
        if self.purger is not None:
            self.purger.request_preview(opt)

    # =========================================================================
    # 4) Onay ekranı
    # =========================================================================
    def _build_confirm(self, f):
        wrap = ctk.CTkFrame(f, fg_color="transparent")
        wrap.pack(fill="both", expand=True, padx=40, pady=26)
        head = ctk.CTkFrame(wrap, fg_color="transparent")
        head.pack(fill="x", pady=(0, 14))
        self.icon(head, "", 24).pack(side="left", padx=(0, 10))       # onay işareti
        self.label(head, "İşlemi onayla", 22, True).pack(side="left")
        self.sum_card = self.card(wrap)
        self.sum_card.pack(fill="x")
        self.target_row = ctk.CTkFrame(self.sum_card, fg_color="transparent")     # kullanıcı modunda hedef kimliği
        self.target_avatar = ctk.CTkLabel(self.target_row, text="?", width=48, height=48, corner_radius=24,
                                          fg_color=ACCENT, font=self.F(20, True), text_color="#fff")
        self.target_avatar.pack(side="left")
        self.target_name = self.label(self.target_row, "", 15, True)
        self.target_name.pack(side="left", padx=12)
        self.sum_rows = ctk.CTkFrame(self.sum_card, fg_color="transparent")
        self.sum_rows.pack(fill="x", padx=18, pady=16)

        warn = ctk.CTkFrame(wrap, fg_color="#2b1618", corner_radius=12, border_width=1, border_color="#7a2b2f")
        warn.pack(fill="x", pady=(14, 0))
        self.label(warn, "Bu işlem geri alınamaz. Silinen mesajlar Discord'dan kalıcı olarak kaldırılır. "
                         "Onay vermeden hiçbir mesaj silinmez.", 12, color="#ffb3b6",
                   wraplength=800, justify="left").pack(anchor="w", padx=16, pady=12)
        self.ack_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(wrap, text="Bu işlemin geri alınamayacağını anladım ve onaylıyorum",
                        variable=self.ack_var, command=self._update_start_state, fg_color=DANGER,
                        hover_color=DANGER_HOVER, text_color=TEXT, font=self.F(13)).pack(anchor="w", pady=(14, 0))

        nav = ctk.CTkFrame(wrap, fg_color="transparent")
        nav.pack(fill="x", pady=(16, 0))
        self.btn(nav, "← Geri", lambda: self.show("scope"), "ghost", width=120).pack(side="left")
        self.start_btn = self.btn(nav, "Silmeyi başlat", self._on_begin, "danger", width=200)
        self.start_btn.pack(side="right")
        self.start_btn.configure(state="disabled")

    @staticmethod
    def _fmt_local(dt):
        return dt.astimezone().strftime("%d.%m.%Y %H:%M")

    def _fill_confirm(self):
        for w in self.sum_rows.winfo_children():
            w.destroy()
        self.ack_var.set(False)
        self.target_row.pack_forget()
        o = self.opt
        rows = [("Hesap", f"{self.account['display_name'] or self.account['username']} (@{self.account['username']})"),
                ("İşlem türü", MODE_LABELS[o["mode"]])]
        if o["mode"] == "user":
            rows.append(("Hedef kullanıcı ID", str(o["user_id"])))
        if o["mode"] == "range":
            a = self._fmt_local(o["after"]) if o.get("after") else "—"
            b = self._fmt_local(o["before"]) if o.get("before") else "—"
            rows.append(("Tarih aralığı", f"{a}  →  {b}  ({local_tz_label()})"))
        rows.append(("Kapsam", "DM'ler" + (" + grup DM'leri" if o["include_groups"] else "")))
        rows.append(("Silinecekler", "Yalnızca senin gönderdiğin mesajlar"))
        rows.append(("Hız", f"{o['speed']} · en az {o['delay']:.1f} sn (rate limit olursa otomatik artar)"))
        rows.append(("Silinenleri kaydet", "Evet" if o["save_deleted"] else "Hayır"))
        rows.append(("Tahmini kapsam", "hesaplanıyor..."))
        self._scope_value_lbl = None
        for i, (k, v) in enumerate(rows):
            self.label(self.sum_rows, k, 12, color=MUTED).grid(row=i, column=0, sticky="nw", padx=(0, 20), pady=3)
            lbl = self.label(self.sum_rows, v, 13, wraplength=560, justify="left")
            lbl.grid(row=i, column=1, sticky="w", pady=3)
            if k == "Tahmini kapsam":
                self._scope_value_lbl = lbl
        self._update_start_state()

    def _on_preview(self, p):
        self.preview = p
        n = p.get("channels")
        if self._scope_value_lbl is None:
            return
        if n is None:
            txt = "hesaplanamadı (mesaj sayısı önceden bilinmiyor)"
        elif n == 0:
            txt = ("Bu ID ile açık bir DM sohbeti bulunamadı." if self.opt.get("mode") == "user"
                   else "İşlenecek sohbet bulunamadı.")
        elif self.opt.get("mode") == "user" and p.get("target"):
            txt = f"1 sohbet: {p['target']} (mesaj sayısı önceden bilinmiyor)"
        else:
            txt = f"{n} sohbet taranacak (mesaj sayısı önceden bilinmiyor)"
        self._scope_value_lbl.configure(text=txt)
        if self.opt.get("mode") == "user" and p.get("target"):
            # Hedef kullanıcının adı/avatarı: Discord'un kendi istemci verisinden (üçüncü taraf servis yok)
            name = p.get("target_display") or p["target"]
            self.target_name.configure(text=f"{name}   ·   {p['target']}")
            self.target_avatar.configure(text=(name[:1] or "?").upper(), image=None)
            self._target_img = self._avatar_image(p.get("target_avatar"), 48)
            if self._target_img is not None:
                self.target_avatar.configure(image=self._target_img, text="")
            self.target_row.pack(fill="x", padx=18, pady=(16, 0), before=self.sum_rows)
        self._update_start_state()

    def _update_start_state(self):
        ok = bool(self.ack_var.get()) and self.preview is not None and bool(self.preview.get("channels"))
        self.start_btn.configure(state="normal" if ok else "disabled")

    def _on_begin(self):
        if self.purger is None or self.phase != "ready":
            return
        if not (self.ack_var.get() and self.preview and self.preview.get("channels")):
            return
        self._reset_run_view()
        self.phase = "running"
        self._run_started = time.monotonic()
        self.show("run")
        self.purger.begin(self.opt)       # taban bekleme opt["delay"] ile motora iletilir

    # =========================================================================
    # 5) Canlı işlem ekranı
    # =========================================================================
    def _build_run(self, f):
        wrap = ctk.CTkFrame(f, fg_color="transparent")
        wrap.pack(fill="both", expand=True, padx=32, pady=22)

        head = ctk.CTkFrame(wrap, fg_color="transparent")
        head.pack(fill="x")
        self.run_avatar = ctk.CTkLabel(head, text="?", width=40, height=40, corner_radius=20,
                                       fg_color=ACCENT, font=self.F(16, True), text_color="#fff")
        self.run_avatar.pack(side="left", padx=(0, 12))
        titles = ctk.CTkFrame(head, fg_color="transparent")
        titles.pack(side="left")
        self.run_title = self.label(titles, "", 18, True)
        self.run_title.pack(anchor="w")
        self.run_acc = self.label(titles, "", 12, color=MUTED)
        self.run_acc.pack(anchor="w")
        self.run_pill = self.pill(head, "çalışıyor", ACCENT)
        self.run_pill.pack(side="right")

        grid = ctk.CTkFrame(wrap, fg_color="transparent")
        grid.pack(fill="x", pady=(10, 0))
        self.stat = {}
        keys = (("dm", "İşlenen sohbet"), ("scanned", "Taranan mesaj"), ("deleted", "Silinen"),
                ("failed", "Hata"), ("delay", "Geçerli bekleme"), ("rl", "Rate limit (429)"),
                ("speed", "Hız ayarı"), ("time", "Geçen süre"))
        for i in range(4):
            grid.grid_columnconfigure(i, weight=1, uniform="s")
        for i, (key, title) in enumerate(keys):
            c = self.card(grid)
            c.grid(row=i // 4, column=i % 4, sticky="nsew", padx=4, pady=4)
            self.label(c, title, 11, color=MUTED).pack(anchor="w", padx=12, pady=(8, 0))
            v = self.label(c, "0", 17, True)
            v.pack(anchor="w", padx=12, pady=(0, 8))
            self.stat[key] = v

        self.progress = ctk.CTkProgressBar(wrap, progress_color=ACCENT, fg_color=SURFACE2, height=10)
        self.progress.set(0)
        self.progress.pack(fill="x", pady=(10, 4))
        self.last_lbl = self.label(wrap, "Son işlem: —", 12, color=MUTED, anchor="w", justify="left")
        self.last_lbl.pack(fill="x", pady=(0, 6))

        self.log_box = ctk.CTkTextbox(wrap, state="disabled", wrap="word", fg_color=SURFACE,
                                      border_width=1, border_color=BORDER, text_color=TEXT,
                                      font=ctk.CTkFont(family="Consolas", size=12))
        self.log_box.pack(fill="both", expand=True)

        self.result_lbl = self.label(wrap, "", 13, True, wraplength=860, justify="left")
        self.result_lbl.pack(anchor="w", pady=(8, 0))

        nav = ctk.CTkFrame(wrap, fg_color="transparent")
        nav.pack(fill="x", pady=(8, 0))
        self.stop_btn = self.btn(nav, "Durdur", self._on_stop, "danger", width=130)
        self.stop_btn.pack(side="left")
        self.btn(nav, "Logları temizle", self._clear_log, "ghost", width=140).pack(side="left", padx=8)
        self.btn(nav, "Logları dışa aktar", self._export_log, "ghost", width=150).pack(side="left")
        self.home_btn = self.btn(nav, "Başa dön", lambda: self._reset_welcome(), "ghost", width=120)
        self.home_btn.pack(side="right")
        self.home_btn.configure(state="disabled")

    def _reset_run_view(self):
        name = self.account["display_name"] or self.account["username"]
        self.run_title.configure(text=MODE_LABELS[self.opt["mode"]])
        self.run_acc.configure(text=f"Hesap: {name} (@{self.account['username']})")
        self.run_avatar.configure(text=(name[:1] or "?").upper(), image=None)
        small = self._avatar_image(self.account.get("avatar"), 40)
        self._run_avatar_img = small
        if small is not None:
            self.run_avatar.configure(image=small, text="")
        self._set_pill("çalışıyor")
        for k, v in (("dm", "0 / 0"), ("scanned", "0"), ("deleted", "0"), ("failed", "0"),
                     ("delay", f"{self.opt['delay']:.1f} sn"), ("rl", "0"), ("time", "00:00:00"),
                     ("speed", self.opt["speed"])):
            self.stat[k].configure(text=v)
        self.last_lbl.configure(text="Son işlem: —")
        for k in INT_STATS:
            self._stat_target[k] = self._stat_shown[k] = 0
        if self._finish_tween is not None:
            self._finish_tween.cancel()
        self._prog_target = self._prog_shown = 0.0
        self.progress.configure(progress_color=ACCENT)
        self.progress.set(0)
        self.result_lbl.configure(text="")
        self.stop_btn.configure(state="normal")
        self.home_btn.configure(state="disabled")

    def _set_pill(self, state):
        self.run_pill.configure(text=f"  ●  {state}  ", fg_color=STATE_COLORS.get(state, MUTED))

    def _append_log(self, text):
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{stamp}] {text}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def _export_log(self):
        path = filedialog.asksaveasfilename(defaultextension=".txt", initialfile="DMCleanerWT_log.txt",
                                            filetypes=[("Metin", "*.txt")])
        if path:
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(self.log_box.get("1.0", "end"))
            except OSError as exc:
                messagebox.showerror(APP_TITLE, f"Log yazılamadı: {exc}")

    def _on_stop(self):
        if self.purger is not None:
            self._append_log("Durduruluyor... (yeni silme isteği gönderilmiyor)")
            self.stop_btn.configure(state="disabled")
            self.purger.stop()

    def _tick(self):
        if self._closing:
            return
        if self.phase == "running" and self._run_started and not self._hidden():
            self.stat["time"].configure(text=fmt_duration(time.monotonic() - self._run_started))
        self.after(500, self._tick)

    # =========================================================================
    # Olay kuyruğu (işçi thread -> arayüz)
    # =========================================================================
    def _poll_events(self):
        if self._closing:
            return
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "log":
                    self._append_log(ev[1])
                elif kind == "account":
                    self.phase = "ready"
                    self._show_account(ev[1])
                    self.conn_pill.configure(text="  ●  Bağlı  ", fg_color=SUCCESS)
                    self.show("home")
                    self._home_intro()
                elif kind == "auth_failed":
                    self._append_log(ev[1])
                    self.purger = None
                    self._reset_welcome(ev[1])
                elif kind == "preview":
                    self._on_preview(ev[1])
                elif kind == "progress":
                    s = ev[1]
                    self.stat["dm"].configure(text=f"{s['dm_i']} / {s['dm_total']}")
                    self.stat["delay"].configure(text=f"{s['delay']:.1f} sn")
                    self._stat_target["scanned"] = s["scanned"]
                    self._stat_target["deleted"] = s["deleted"]
                    self._stat_target["failed"] = s["failed"]
                    self._stat_target["rl"] = s["rl_hits"]
                    self._prog_target = s["dm_i"] / s["dm_total"] if s["dm_total"] else 0.0
                    if s.get("last"):
                        self.last_lbl.configure(text=f"Son işlem: {s['last']}")
                elif kind == "done":
                    self._on_done(ev[1], ev[2], ev[3])
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def _on_done(self, deleted, scanned, failed):
        p = self.purger
        self.purger = None
        if self.phase == "running":
            state = p.state if p is not None else "tamamlandı"
            self.phase = "finished"
            self._set_pill(state)
            for k, v in (("deleted", deleted), ("scanned", scanned), ("failed", failed)):
                self._stat_target[k] = self._stat_shown[k] = v
                self.stat[k].configure(text=str(v))
            if state == "tamamlandı":
                self._prog_target = 1.0
            end_color = {"tamamlandı": SUCCESS, "durduruldu": WARN, "hata": DANGER}.get(state, ACCENT)
            self._finish_tween = Tween(
                self, 450, lambda t: self.progress.configure(progress_color=lerp_color(ACCENT, end_color, t)),
                easing=ease_in_out).start()
            self.result_lbl.configure(
                text=f"İşlem {state}. Taranan: {scanned} · Silinen: {deleted} · Hata: {failed}. "
                     f"Özet: {SUMMARY_FILE}")
            self.stop_btn.configure(state="disabled")
            self.home_btn.configure(state="normal")
        elif self.phase in ("ready", "connecting"):
            self._reset_welcome("Bağlantı kapatıldı." if self.phase == "ready" else "")

    def _on_close(self):
        """Pencere kapanışı: bağlantı kurulurken de, işlem sürerken de önce güvenli durdur, sonra kapat."""
        self._closing = True
        if self.purger is not None:
            self.purger.stop()
            self.purger.thread.join(timeout=3)
        self.destroy()

    def report_callback_exception(self, exc, val, tb):
        _excepthook(exc, val, tb)


def main():
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    App().mainloop()


if __name__ == "__main__":
    main()
