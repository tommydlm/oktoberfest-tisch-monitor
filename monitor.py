import hashlib
import json
import os
import sys

import requests
from playwright.sync_api import sync_playwright

URL = "https://oktoberfest-booking.com/de"
TARGET_DATES = {"25.09.2026", "26.09.2026", "27.09.2026"}
SEEN_FILE = "seen.json"
ERROR_FILE = "error_count.txt"
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen), f, indent=2, ensure_ascii=False)


def load_error_count():
    if os.path.exists(ERROR_FILE):
        try:
            return int(open(ERROR_FILE).read().strip() or "0")
        except ValueError:
            return 0
    return 0


def save_error_count(n):
    with open(ERROR_FILE, "w") as f:
        f.write(str(n))


def post_discord(embeds):
    if not WEBHOOK_URL:
        print("No DISCORD_WEBHOOK_URL set, skipping post")
        return
    for i in range(0, len(embeds), 10):
        chunk = embeds[i:i + 10]
        resp = requests.post(WEBHOOK_URL, json={"embeds": chunk}, timeout=15)
        if resp.status_code >= 300:
            print(f"Discord post failed: {resp.status_code} {resp.text}")


def post_warning(text):
    if not WEBHOOK_URL:
        return
    requests.post(WEBHOOK_URL, json={"content": text}, timeout=15)


def dump_debug(page, label):
    os.makedirs("debug", exist_ok=True)
    try:
        page.screenshot(path=f"debug/{label}.png", full_page=True)
    except Exception as e:
        print(f"screenshot failed: {e}")
    try:
        with open(f"debug/{label}.html", "w") as f:
            f.write(page.content())
    except Exception as e:
        print(f"html dump failed: {e}")


def scrape_page_text():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            locale="de-DE",
            viewport={"width": 1366, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            extra_http_headers={"Accept-Language": "de-DE,de;q=0.9"},
        )
        page = context.new_page()
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)

            page.click("text=Reservierung kaufen", timeout=15000)
            page.wait_for_timeout(1500)

            search_btn = page.locator("text=Suche").first
            if search_btn.count():
                search_btn.click()

            page.wait_for_selector("text=Details anzeigen", timeout=20000)
            page.wait_for_timeout(2000)

            text = page.locator("main").inner_text()
        except Exception:
            dump_debug(page, "failure")
            raise
        browser.close()
        return text


def split_cards(full_text):
    parts = full_text.split("Infos zum Zelt")[1:]
    cards = []
    for part in parts:
        end = part.find("Der Reservierungsalarm")
        if end != -1:
            part = part[:end]
        cards.append(part)
    return cards


def parse_offer(card_text):
    lines = [l.strip() for l in card_text.split("\n") if l.strip()]
    in_progress = any("befindet sich derzeit im Kaufprozess" in l for l in lines)
    lines = [l for l in lines if "befindet sich derzeit im Kaufprozess" not in l]

    date = next((l for l in lines if any(d in l for d in TARGET_DATES)), None)
    if not date:
        return None

    zelt = lines[0] if lines else None
    tageszeit = next((l for l in ("Vormittag", "Mittag", "Nachmittag", "Abend") if l in lines), None)
    uhrzeit = next((l for l in lines if "Uhr" in l and "-" in l), None)
    personen = next((l for l in lines if "Person" in l), None)
    tische = next((l for l in lines if "Tisch(e)" in l), None)

    preise = [l for l in lines if l.startswith("€")]
    preis = preise[-1] if preise else None

    if not all([zelt, date, tageszeit, uhrzeit, personen, tische, preis]):
        return None

    return {
        "zelt": zelt,
        "date": date,
        "tageszeit": tageszeit,
        "uhrzeit": uhrzeit,
        "personen": personen,
        "tische": tische,
        "preis": preis,
        "in_progress": in_progress,
    }


def make_key(o):
    raw = "|".join([o["zelt"], o["date"], o["tageszeit"], o["uhrzeit"], o["personen"], o["tische"], o["preis"]])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def main():
    seen = load_seen()
    error_count = load_error_count()

    try:
        full_text = scrape_page_text()
        cards = split_cards(full_text)
    except Exception as e:
        error_count += 1
        save_error_count(error_count)
        print(f"Scrape failed ({error_count} in a row): {e}")
        if error_count >= 3:
            post_warning("⚠️ Tisch-Monitor: 3+ fehlgeschlagene Läufe in Folge, bitte prüfen.")
            save_error_count(0)
        sys.exit(0)

    parsed = [p for p in (parse_offer(c) for c in cards) if p]
    target = [o for o in parsed if o["date"] in TARGET_DATES]

    save_error_count(0)

    if not target:
        print("No matching offers this run.")
        return

    new_offers = []
    for o in target:
        key = make_key(o)
        if key not in seen:
            seen.add(key)
            new_offers.append(o)

    if new_offers:
        embeds = []
        for o in new_offers:
            fields = [
                {"name": "Datum", "value": o["date"], "inline": True},
                {"name": "Tageszeit", "value": o["tageszeit"], "inline": True},
                {"name": "Uhrzeit", "value": o["uhrzeit"], "inline": True},
                {"name": "Personen", "value": o["personen"], "inline": True},
                {"name": "Tische", "value": o["tische"], "inline": True},
                {"name": "Preis", "value": o["preis"], "inline": True},
            ]
            if o["in_progress"]:
                fields.append({"name": "Hinweis", "value": "⚠️ Ein Käufer ist bereits im Kaufprozess", "inline": False})
            embeds.append({
                "title": o["zelt"],
                "color": 3066993,
                "fields": fields,
                "url": "https://oktoberfest-booking.com/de/reseller-angebote",
            })
        post_discord(embeds)
        print(f"Posted {len(new_offers)} new offer(s).")
        save_seen(seen)
    else:
        print("No new offers.")


if __name__ == "__main__":
    main()
