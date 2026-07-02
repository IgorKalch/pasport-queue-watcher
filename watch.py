#!/usr/bin/env python3
"""
Pasport e-queue watcher.

Перевіряє сторінки електронної черги pasport.org.ua на наявність вільних
місць для запису й надсилає сповіщення в Telegram, коли місця зʼявляються.

Додатково повідомляє про «здоровʼя» самого автомата:
  • якщо не може прочитати сторінку (сайт лежить / Cloudflare-блок / помилка) —
    надсилає «⚠️ не можу перевірити» (один раз, коли ламається);
  • коли знову запрацювало — «✅ знову працює»;
  • раз на HEARTBEAT_HOURS годин — «я живий» зі зведенням стану.

Керується через змінні оточення (див. README.md).
"""

import os
import sys
import json
import time
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse

import requests

# ----------------------------------------------------------------------------
# Читання змінних оточення (стійке до порожніх значень)
# ----------------------------------------------------------------------------
# Увага: у GitHub Actions невизначена Repository variable підставляється як
# ПОРОЖНІЙ рядок (""), а не як «відсутня». Тому os.environ.get(name, default)
# поверне "" замість default. Ці хелпери повертають default на порожньому/
# некоректному значенні, щоб не було падінь (int(""))  і «порожніх» списків.

def env_str(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v if v is not None and v.strip() != "" else default


def env_int(name: str, default: int) -> int:
    try:
        return int(env_str(name, str(default)))
    except (TypeError, ValueError):
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(env_str(name, str(default)))
    except (TypeError, ValueError):
        return default


# ----------------------------------------------------------------------------
# Конфігурація (через змінні оточення)
# ----------------------------------------------------------------------------

# Список сторінок через кому.
URLS = [
    u.strip()
    for u in env_str(
        "WATCH_URLS",
        "https://wroclaw.pasport.org.ua/solutions/e-queue",
    ).split(",")
    if u.strip()
]

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# Фрази, які означають, що вільних місць НЕМАЄ (у нижньому регістрі).
# Якщо на сторінці зустрічається хоча б одна з них — вважаємо, що місць немає.
# Підтверджений реальний текст сайту (07.2026): «Наразі всі місця зайняті.»
UNAVAILABLE_PHRASES = [
    p.strip().lower()
    for p in env_str(
        "UNAVAILABLE_PHRASES",
        "|".join(
            [
                "всі місця зайняті",       # ← підтверджено на wroclaw (07.2026)
                "усі місця зайняті",
                "наразі всі місця зайняті",
                "немає вільних",
                "немає вільних місць",
                "немає доступних",
                "наразі немає",
                "місця відсутні",
                "запис відсутній",
                "запис тимчасово недоступний",
                "немає активних",
                "no available",
                "no free slots",
                "no slots",
            ]
        ),
    ).split("|")
    if p.strip()
]

# Позитивні маркери — ознаки, що на сторінці Є активна форма запису (тобто
# місця, найпевніше, доступні). Підтверджено на krakow (07.2026): форма
# рендериться як Alpine-компонент queueForm()/qlogickFormTotoro(...).
# Шукаємо в СИРОМУ HTML (це атрибути, а не видимий текст).
AVAILABLE_MARKERS = [
    m.strip().lower()
    for m in env_str(
        "AVAILABLE_MARKERS",
        "|".join(
            [
                "queueform",
                "qlogickform",
            ]
        ),
    ).split("|")
    if m.strip()
]

# «Маркери» сторінки — фрази, які МАЮТЬ бути на коректно завантаженій сторінці.
# Якщо жодної немає — сторінка, ймовірно, не завантажилась (капча, помилка,
# порожній JS). У такому разі ми вважаємо це ПОМИЛКОЮ (а не «місць немає»).
PAGE_MARKERS = [
    m.strip().lower()
    for m in env_str(
        "PAGE_MARKERS",
        "|".join(
            [
                "черг",          # черга / черги / електронна черга
                "запис",         # запис / записатися
                "e-queue",
                "послуг",        # послуга / послуги
            ]
        ),
    ).split("|")
    if m.strip()
]

# Ознаки Cloudflare-челенджу («Just a moment…»). Якщо трапилось — сторінку
# прочитати не вдалось (це стан ПОМИЛКИ, а не «місць немає»).
CHALLENGE_MARKERS = [
    "just a moment",
    "enable javascript and cookies",
    "cdn-cgi/challenge-platform",
    "__cf_chl",
    "cf-chl",
]

# Мінімум хвилин між повторними сповіщеннями «є місця» для однієї сторінки,
# доки місця залишаються доступними (щоб не спамити).
COOLDOWN_MIN = env_int("COOLDOWN_MIN", 30)

# Скільки поспіль невдалих перевірок сторінки має статись, перш ніж надіслати
# сповіщення «не можу перевірити». На cron кожні 5 хв: 3 → бити на сполох
# приблизно через 15 хв стабільних збоїв (щоб не реагувати на разові збої).
ERROR_ALERT_AFTER = env_int("ERROR_ALERT_AFTER", 3)

# Раз на скільки годин надсилати сповіщення «я живий» (автомат працює).
# 0 — вимкнути періодичний heartbeat.
HEARTBEAT_HOURS = env_float("HEARTBEAT_HOURS", 24)

# Файл стану (памʼятає стан під час попередньої перевірки).
STATE_FILE = Path(env_str("STATE_FILE", "state.json"))

# Використовувати повноцінний браузер (Playwright) для рендерингу JS.
# Сторінки pasport.org.ua рендеряться на сервері (SSR), тож "0" (простий
# HTTP-запит) працює, швидший і дешевший. "1" — браузер (запасний варіант,
# інколи краще проходить Cloudflare, але значно повільніший).
USE_BROWSER = env_str("USE_BROWSER", "0") == "1"

# Скільки мс чекати після завантаження, щоб JS встиг підвантажити місця
# (лише для режиму браузера).
RENDER_WAIT_MS = env_int("RENDER_WAIT_MS", 4000)

# Режим циклу для локального запуску: якщо > 0 — перевіряти нескінченно
# з такою паузою (у секундах). 0 — виконатись один раз (для GitHub Actions).
LOOP_SECONDS = env_int("LOOP_SECONDS", 0)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Статуси перевірки сторінки.
AVAILABLE = "available"      # схоже, є вільні місця
UNAVAILABLE = "unavailable"  # місць немає
ERROR = "error"              # не вдалося прочитати сторінку

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("watcher")


# ----------------------------------------------------------------------------
# Стан
# ----------------------------------------------------------------------------

META_KEY = "__meta__"


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            log.warning("Не вдалося прочитати %s — починаю з чистого стану", STATE_FILE)
    return {}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        log.error("Не вдалося зберегти стан: %s", e)


# ----------------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------------

def send_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error(
            "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не задані. "
            "Повідомлення (не надіслане):\n%s",
            text,
        )
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "false",
            },
            timeout=30,
        )
        if r.status_code != 200:
            log.error("Telegram відповів %s: %s", r.status_code, r.text)
            return False
        log.info("Сповіщення надіслано в Telegram")
        return True
    except Exception as e:
        log.error("Помилка надсилання в Telegram: %s", e)
        return False


# ----------------------------------------------------------------------------
# Завантаження сторінки (повертає СИРИЙ HTML)
# ----------------------------------------------------------------------------

def fetch_html_requests(url: str) -> str:
    """Простий HTTP-запит, повертає сирий HTML."""
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8"}
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.text


def fetch_html_browser(url: str) -> str:
    """Завантаження в headless-браузері (виконує JavaScript), повертає HTML."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        context = browser.new_context(locale="uk-UA", user_agent=USER_AGENT)
        page = context.new_page()
        try:
            page.goto(url, wait_until="networkidle", timeout=60000)
        except Exception:
            # networkidle іноді не настає — беремо те, що встигло завантажитись
            log.warning("networkidle не настав для %s, продовжую", url)
        page.wait_for_timeout(RENDER_WAIT_MS)
        html = page.content()
        browser.close()
        return html


def fetch_html(url: str) -> str:
    return fetch_html_browser(url) if USE_BROWSER else fetch_html_requests(url)


# ----------------------------------------------------------------------------
# Класифікація сторінки
# ----------------------------------------------------------------------------

def visible_text(html: str) -> str:
    """Витягує видимий текст зі сторінки (без script/style/noscript)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text(separator=" ", strip=True)


def looks_like_challenge(html: str) -> bool:
    low = html.lower()
    return any(m in low for m in CHALLENGE_MARKERS)


def page_loaded(text: str) -> bool:
    if not PAGE_MARKERS:
        return True
    low = text.lower()
    return any(m in low for m in PAGE_MARKERS)


def matched_unavailable(text: str) -> str | None:
    low = text.lower()
    for phrase in UNAVAILABLE_PHRASES:
        if phrase and phrase in low:
            return phrase
    return None


def has_available_marker(html: str) -> bool:
    low = html.lower()
    return any(m in low for m in AVAILABLE_MARKERS)


def classify(html: str) -> dict:
    """
    Класифікує сторінку → dict(status, confidence, detail).

    Логіка навмисне зміщена в бік «не пропустити місце»:
      • челендж/не завантажилось          → ERROR;
      • є форма запису й немає «зайнято»    → AVAILABLE (висока впевненість);
      • є «зайнято» й немає форми           → UNAVAILABLE (висока впевненість);
      • немає ні того, ні того             → AVAILABLE (низька впевненість —
                                             краще зайвий раз перевірити вручну);
      • є і форма, і «зайнято» (конфлікт)  → AVAILABLE (форма важливіша).
    """
    if looks_like_challenge(html):
        return {"status": ERROR, "confidence": "high",
                "detail": "Cloudflare-челендж (Just a moment…)"}

    text = visible_text(html)
    if not page_loaded(text):
        return {"status": ERROR, "confidence": "high",
                "detail": f"немає маркерів сторінки (довжина тексту {len(text)})"}

    blocked = matched_unavailable(text)
    form = has_available_marker(html)

    if form and not blocked:
        return {"status": AVAILABLE, "confidence": "high", "detail": "видно форму запису"}
    if blocked and not form:
        return {"status": UNAVAILABLE, "confidence": "high", "detail": f"«{blocked}»"}
    if not blocked and not form:
        return {"status": AVAILABLE, "confidence": "low",
                "detail": "немає ні «зайнято», ні форми — перевір вручну"}
    # form and blocked — суперечливо, форму вважаємо важливішою
    return {"status": AVAILABLE, "confidence": "low",
            "detail": "видно форму, але є й фраза «зайнято» — перевір вручну"}


# ----------------------------------------------------------------------------
# Допоміжне для повідомлень
# ----------------------------------------------------------------------------

def humanize_ago(iso: str | None, now: datetime) -> str:
    if not iso:
        return "?"
    try:
        dt = datetime.fromisoformat(iso)
    except Exception:
        return "?"
    mins = int((now - dt).total_seconds() // 60)
    if mins < 1:
        return "щойно"
    if mins < 60:
        return f"{mins} хв тому"
    hours = mins // 60
    return f"{hours} год тому"


# ----------------------------------------------------------------------------
# Логіка перевірки однієї сторінки
# ----------------------------------------------------------------------------

def check_url(url: str, state: dict) -> None:
    now = datetime.now(timezone.utc)
    host = urlparse(url).netloc

    try:
        html = fetch_html(url)
        result = classify(html)
    except Exception as e:
        result = {"status": ERROR, "confidence": "high", "detail": str(e)}

    status = result["status"]
    detail = result["detail"]

    prev = state.get(url, {})
    prev_status = prev.get("status")
    prev_error_alerted = prev.get("error_alerted", False)

    log.info("[%s] статус: %s (%s) — було: %s", host, status, detail, prev_status)

    # --- ПОМИЛКА: не змогли прочитати сторінку --------------------------------
    if status == ERROR:
        streak = prev.get("error_streak", 0) + 1
        prev["error_streak"] = streak
        # бити на сполох лише раз, коли ламається (після ERROR_ALERT_AFTER збоїв).
        # Прапорець ставимо лише якщо повідомлення реально відправилось — інакше
        # разовий збій Telegram не проковтне єдине сповіщення.
        if streak >= ERROR_ALERT_AFTER and not prev_error_alerted:
            sent = send_telegram(
                "⚠️ <b>Автомат не може перевірити сторінку.</b>\n"
                f"{host}\n{url}\n\n"
                f"Причина: {detail}\n"
                f"Невдалих спроб поспіль: {streak}.\n"
                "Повідомлю, коли знову запрацює."
            )
            if sent:
                prev["error_alerted"] = True
        prev["status"] = status
        prev["last_detail"] = detail
        prev["last_checked"] = now.isoformat()
        state[url] = prev
        return

    # --- УСПІШНО прочитали: спершу — відновлення після помилки -----------------
    if prev_error_alerted:
        send_telegram(
            "✅ <b>Автомат знову працює.</b>\n"
            f"{host}\n"
            f"Поточний стан: {'є місця' if status == AVAILABLE else 'місць немає'}."
        )
    prev["error_streak"] = 0
    prev["error_alerted"] = False

    available = status == AVAILABLE
    prev_available = prev_status == AVAILABLE
    last_notified = prev.get("last_available_notified")

    # --- Сповіщення «є місця» -------------------------------------------------
    should_notify = False
    if available:
        if not prev_available:
            should_notify = True  # перехід «немає/помилка» → «є»
        elif last_notified:
            try:
                last_dt = datetime.fromisoformat(last_notified)
                if now - last_dt >= timedelta(minutes=COOLDOWN_MIN):
                    should_notify = True  # нагадування, місця ще є
            except Exception:
                should_notify = True
        else:
            should_notify = True

    if should_notify:
        note = "" if result["confidence"] == "high" else f"\n⚠️ Невпевнено: {detail}"
        send_telegram(
            "🟢 <b>Можливо, зʼявилися вільні місця для запису!</b>\n"
            f"{host}\n{url}\n"
            f"{note}\n\n"
            "Перевір сторінку і спробуй записатися якнайшвидше."
        )
        prev["last_available_notified"] = now.isoformat()

    prev["status"] = status
    prev["available"] = available
    prev["last_checked"] = now.isoformat()
    prev["last_detail"] = detail
    state[url] = prev


# ----------------------------------------------------------------------------
# Heartbeat «я живий»
# ----------------------------------------------------------------------------

def status_emoji(status: str | None) -> str:
    return {AVAILABLE: "🟢", UNAVAILABLE: "🔴", ERROR: "⚠️"}.get(status, "❔")


def status_word(status: str | None) -> str:
    return {AVAILABLE: "є місця", UNAVAILABLE: "місць немає",
            ERROR: "помилка перевірки"}.get(status, "невідомо")


def maybe_heartbeat(state: dict, now: datetime) -> None:
    if HEARTBEAT_HOURS <= 0:
        return
    meta = state.get(META_KEY, {})
    last_hb = meta.get("last_heartbeat")

    first_run = last_hb is None
    due = False
    if not first_run:
        try:
            due = now - datetime.fromisoformat(last_hb) >= timedelta(hours=HEARTBEAT_HOURS)
        except Exception:
            due = True

    if not (first_run or due):
        return

    lines = []
    for url in URLS:
        st = state.get(url, {})
        host = urlparse(url).netloc
        lines.append(
            f"{status_emoji(st.get('status'))} {host} — {status_word(st.get('status'))} "
            f"(перевірено {humanize_ago(st.get('last_checked'), now)})"
        )
    body = "\n".join(lines) if lines else "(немає сторінок для перевірки)"

    title = "🤖 <b>Автомат запущено. Стежу за сторінками:</b>" if first_run \
        else "🤖 <b>Автомат працює.</b>"

    # last_heartbeat оновлюємо лише при успішній відправці, щоб при збої Telegram
    # heartbeat лишався «прострочений» і повторив спробу наступного разу.
    if send_telegram(f"{title}\n\n{body}"):
        meta["last_heartbeat"] = now.isoformat()
        state[META_KEY] = meta


# ----------------------------------------------------------------------------
# Прогін
# ----------------------------------------------------------------------------

def run_once() -> None:
    log.info(
        "Перевіряю %d сторінк(и). Режим: %s",
        len(URLS),
        "браузер" if USE_BROWSER else "HTTP",
    )
    state = load_state()
    for url in URLS:
        check_url(url, state)
    maybe_heartbeat(state, datetime.now(timezone.utc))
    save_state(state)


def main() -> int:
    if LOOP_SECONDS > 0:
        log.info("Режим циклу: перевірка кожні %d с. Зупинка — Ctrl+C.", LOOP_SECONDS)
        while True:
            try:
                run_once()
            except KeyboardInterrupt:
                log.info("Зупинено користувачем")
                return 0
            except Exception as e:
                log.error("Неочікувана помилка: %s", e)
            time.sleep(LOOP_SECONDS)
    else:
        run_once()
    return 0


if __name__ == "__main__":
    sys.exit(main())
