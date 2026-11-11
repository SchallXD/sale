#!/usr/bin/env python3
"""Linsoul のセールを毎日見張って、サイト（index.html）を更新するスクリプト。

GitHub Actions（.github/workflows/linsoul.yml）から1日1回呼ばれる。

  python scripts/linsoul_sale.py daily   # 価格・在庫の更新、終了判定、開始判定
  python scripts/linsoul_sale.py draft   # 値引き中の全商品で下書きを作る（PR 用ブランチで実行）

daily がすること
  1. linsoul.com の公開商品一覧（products.json）を読み、値引き中の商品数を数えて data/linsoul-history.json に記録
  2. 掲載中の Linsoul 商品の円価格・在庫を更新
  3. セール終了（期間切れ、または値引き商品数がふだんの水準に戻った）なら Linsoul の商品を隠す
  4. セール開始らしいとき（値引き商品の急増、Linsoul_JP の X 投稿）は GITHUB_OUTPUT に sale_detected=true を書く
     → ワークフローが draft を別ブランチで実行し、プルリクエストで知らせる（マージすると公開）

アクセスは robots.txt / agents.md で許可されている公開ページだけ。自動アクセスだと名乗り、
429 が返ったら Retry-After に従って待つ。X は公式 API（X_BEARER_TOKEN があるときだけ）を使う。
"""
import datetime as dt
import io
import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "index.html"
HISTORY = ROOT / "data" / "linsoul-history.json"
IMG_DIR = ROOT / "img" / "linsoul"
STORE = "https://www.linsoul.com"
UA = "schall-sale-bot/1.0 (automated daily price check; +https://schallxd.github.io/sale/)"
JST = dt.timezone(dt.timedelta(hours=9))
NOW = dt.datetime.now(JST)
TODAY = NOW.date().isoformat()

# 開始・終了の判定に使う値
BASELINE_DAYS = 14          # ふだんの水準 = セールでない直近14日の中央値
MIN_BASELINE_DAYS = 5       # これより記録が少ないうちは、商品数だけでは開始と判断しない
JUMP_RATIO, JUMP_ABS = 1.4, 60      # ふだんの 1.4 倍以上 かつ +60 品以上で「急増」
X_JUMP_ABS = 25                     # X に告知があれば +25 品以上で開始と判断
NO_BASELINE_X_MIN = 150             # 記録が少ないうちは、X の告知 + 値引き150品以上で開始と判断
END_MARGIN = 20                     # ふだん +20 品以内に戻った日が2日続いたら終了
X_KEYWORDS = re.compile(r"(セール|SALE|Sale|sale|%\s*OFF|％\s*OFF|%off|割引|値下げ|クーポン|ブラックフライデー|Black\s*Friday|サイバーマンデー|11\.11|618|\bDeals?\b)")

CATEGORY = {
    "In-Ear Monitors": "イヤホン", "Earphones/IEMs": "イヤホン", "Earphones": "イヤホン", "TWS": "イヤホン",
    "Earbuds": "イヤホン", "Headphones": "ヘッドホン", "AMP & DAC": "DAC・アンプ", "DAC": "DAC・アンプ",
    "Amplifier": "DAC・アンプ", "Cable Convertors": "DAC・アンプ", "DAP": "DAC・アンプ",
}
SKIP_TYPES = {"Bundles", "Gift Card", "Gift Cards", ""}


# ---------- 通信 ----------
def fetch(url, binary=False):
    for attempt in range(6):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
                return data if binary else data.decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code in (429, 503):
                wait = int(e.headers.get("Retry-After") or 0) or 10 * (attempt + 1)
                time.sleep(min(wait, 120))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"取得できませんでした: {url}")


def catalog(currency=None):
    """公開商品一覧を全ページ読む。currency='JPY' なら円価格（Linsoul 側で換算したもの）。"""
    out, page = [], 1
    while True:
        q = {"limit": 250, "page": page}
        if currency:
            q["currency"] = currency
        items = json.loads(fetch(f"{STORE}/products.json?{urllib.parse.urlencode(q)}"))["products"]
        if not items:
            return out
        out += items
        page += 1
        time.sleep(1.5)


def best_variant(p):
    vs = [v for v in p["variants"] if v.get("available")] or p["variants"]
    v = min(vs, key=lambda v: float(v["price"]))
    price = float(v["price"])
    cmp = float(v["compare_at_price"]) if v.get("compare_at_price") else None
    return {
        "price": price,
        "compare": cmp if cmp and cmp > price else None,
        "from": len({float(x["price"]) for x in vs}) > 1,
        "available": any(x.get("available") for x in p["variants"]),
        "variant_id": v["id"],
    }


def discounted(p):
    b = best_variant(p)
    return b["available"] and b["compare"] is not None and b["compare"] - b["price"] >= 0.5


# ---------- index.html の読み書き ----------
DATA_RE = re.compile(r'(<script type="application/json" id="site-data">)(.*?)(</script>)', re.S)


def load_site():
    html = INDEX.read_text(encoding="utf-8")
    m = DATA_RE.search(html)
    return html, json.loads(m.group(2))


def save_site(html, data):
    m = DATA_RE.search(html)
    js = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    INDEX.write_text(html[: m.start(2)] + js + html[m.end(2):], encoding="utf-8")


def handle_of(p):
    m = re.search(r"/products/([^/?#]+)", p.get("purchaseUrl", ""))
    return m.group(1) if m and "linsoul.com" in p.get("purchaseUrl", "") else None


def is_linsoul(p):
    return p.get("shop") == "Linsoul"


def shop_conf(data):
    return data["campaign"].setdefault("shops", {}).setdefault("Linsoul", {})


def sale_live(data):
    conf = shop_conf(data)
    if conf.get("end") and NOW > dt.datetime.fromisoformat(conf["end"]):
        return False
    return any(is_linsoul(p) and not p.get("hidden") for p in data["products"])


# ---------- 記録 ----------
def load_history():
    return json.loads(HISTORY.read_text()) if HISTORY.exists() else []


def baseline(history):
    normal = [h["discounted"] for h in history if not h.get("sale")][-BASELINE_DAYS:]
    return (statistics.median(normal), len(normal)) if normal else (None, 0)


# ---------- X ----------
def x_signal():
    """Linsoul_JP の直近3日の投稿からセールらしいものを探す。トークンがなければ何もしない。"""
    token, user = os.environ.get("X_BEARER_TOKEN"), os.environ.get("X_USERNAME", "").lstrip("@")
    if not token or not user:
        return []
    def api(path):
        req = urllib.request.Request(f"https://api.x.com/2/{path}", headers={"Authorization": f"Bearer {token}", "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    try:
        uid = api(f"users/by/username/{urllib.parse.quote(user)}")["data"]["id"]
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        res = api(f"users/{uid}/tweets?max_results=10&exclude=retweets,replies&start_time={since}&tweet.fields=created_at")
    except Exception as e:  # X が使えなくても、ほかの判定は続ける
        print(f"X を読めませんでした: {e}", file=sys.stderr)
        return []
    hits = [t for t in res.get("data", []) if X_KEYWORDS.search(t.get("text", ""))]
    return [{"id": t["id"], "text": t["text"][:280], "url": f"https://x.com/{user}/status/{t['id']}"} for t in hits]


# ---------- 商品データ ----------
def jpy_map(jpy_items):
    return {p["handle"]: best_variant(p) for p in jpy_items}


def apply_prices(entry, usd, jpy):
    entry["salePriceJpy"] = int(round(jpy["price"]))
    entry["regularPriceJpy"] = int(round(jpy["compare"])) if jpy["compare"] else None
    entry["priceFrom"] = usd["from"]
    if usd["compare"]:
        entry["discountPctFixed"] = round((1 - usd["price"] / usd["compare"]) * 10000) / 100
    else:
        entry.pop("discountPctFixed", None)
    entry["discountPct"] = entry.get("discountPctFixed")
    if not usd["available"]:
        entry["autoDiscount"] = "在庫切れ（入荷待ち）・セール価格で表示中"
    elif not usd["compare"]:
        entry["autoDiscount"] = "確認時点では割引なしの価格です"
    else:
        entry["autoDiscount"] = "セール価格で表示中（コード不要）"


def save_image(p):
    """商品画像の1枚目を 400x400 の webp にして img/linsoul/ に置く（すでにあれば何もしない）。"""
    from PIL import Image
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    path = IMG_DIR / f"{p['handle']}.webp"
    rel = f"img/linsoul/{p['handle']}.webp"
    if path.exists():
        return rel
    if not p.get("images"):
        return None
    src = p["images"][0]["src"]
    raw = fetch(src + ("&" if "?" in src else "?") + "width=600", binary=True)
    im = Image.open(io.BytesIO(raw)).convert("RGB")
    im.thumbnail((400, 400))
    canvas = Image.new("RGB", (400, 400), "white")
    canvas.paste(im, ((400 - im.width) // 2, (400 - im.height) // 2))
    canvas.save(path, "WEBP", quality=72)
    time.sleep(0.3)
    return rel


def name_parts(p):
    brand = (p.get("vendor") or "").strip() or p["title"].split(" ")[0]
    name = p["title"].strip()
    if name.lower().startswith(brand.lower() + " "):
        name = name[len(brand) + 1:]
    if name.lower().startswith("x "):
        name = f"{brand} × {name[2:]}"
    return brand, name


# ---------- daily ----------
def daily():
    html, data = load_site()
    usd_items, jpy_items = catalog(), catalog("JPY")
    usd_by, jpy_by = {p["handle"]: p for p in usd_items}, jpy_map(jpy_items)
    count = sum(1 for p in usd_items if discounted(p))
    history = load_history()
    base, base_days = baseline(history)
    xs = x_signal()
    live = sale_live(data)
    conf = shop_conf(data)
    changed, notes = False, []

    # 掲載中の商品の価格・在庫を更新
    if live:
        for e in data["products"]:
            h = handle_of(e)
            if not is_linsoul(e) or e.get("hidden") or not h:
                continue
            if h not in usd_by or h not in jpy_by:
                e["hidden"] = True
                notes.append(f"サイトから消えたので隠しました: {e['brand']} {e['name']}")
                changed = True
                continue
            before = json.dumps(e, ensure_ascii=False, sort_keys=True)
            apply_prices(e, best_variant(usd_by[h]), jpy_by[h])
            if json.dumps(e, ensure_ascii=False, sort_keys=True) != before:
                changed = True
        if changed:
            conf["checkedAt"] = TODAY

    # 終了判定：期間切れ、または（期間不明のとき）ふだんの水準に2日続けて戻った
    ended = False
    if live:
        if conf.get("end") and NOW > dt.datetime.fromisoformat(conf["end"]):
            ended = True
        elif not conf.get("end") and base is not None and count <= base + END_MARGIN:
            prev = history[-1] if history else None
            ended = bool(prev and prev.get("sale") and prev["discounted"] <= base + END_MARGIN)
    if ended:
        for e in data["products"]:
            if is_linsoul(e) and not e.get("hidden"):
                e["hidden"] = True
        conf["lastEnded"] = TODAY
        notes.append("Linsoul のセールが終わったので、Linsoul の商品をすべて隠しました")
        changed, live = True, False

    # 開始判定
    detected, reason = False, ""
    if not live:
        if base is not None and base_days >= MIN_BASELINE_DAYS:
            if count >= base * JUMP_RATIO and count >= base + JUMP_ABS:
                detected, reason = True, f"値引き中の商品が {count} 品（ふだんは {base:.0f} 品前後）"
            elif xs and count >= base + X_JUMP_ABS:
                detected, reason = True, f"Linsoul_JP の X にセールの投稿があり、値引き中の商品が {count} 品（ふだんは {base:.0f} 品前後）"
        elif xs and count >= NO_BASELINE_X_MIN:
            detected, reason = True, f"Linsoul_JP の X にセールの投稿があり、値引き中の商品が {count} 品（ふだんの水準はまだ記録中）"

    # 記録（同じ日は上書き）
    history = [h for h in history if h["date"] != TODAY]
    history.append({"date": TODAY, "discounted": count, "total": len(usd_items), "sale": live or detected, "x_posts": len(xs)})
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    HISTORY.write_text(json.dumps(history, ensure_ascii=False, indent=1) + "\n")

    if changed:
        save_site(html, data)
    report = {"date": TODAY, "discounted": count, "baseline": base, "baseline_days": base_days,
              "live": live, "detected": detected, "reason": reason, "x_posts": xs, "notes": notes}
    (ROOT / "data" / "linsoul-last-run.json").write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=1))
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"sale_detected={'true' if detected else 'false'}\n")


# ---------- draft ----------
def draft():
    """値引き中の全商品で Linsoul の棚を作り直す（PR 用ブランチで実行し、マージで公開）。"""
    html, data = load_site()
    report = json.loads((ROOT / "data" / "linsoul-last-run.json").read_text())
    usd_items, jpy_by = catalog(), jpy_map(catalog("JPY"))
    keep_featured = {handle_of(e) for e in data["products"] if is_linsoul(e) and e.get("featured")}
    data["products"] = [e for e in data["products"] if not is_linsoul(e)]
    next_id = max([int(e["id"]) for e in data["products"] if str(e.get("id", "")).isdigit()] or [0])
    added = 0
    for p in usd_items:
        if p.get("product_type", "") in SKIP_TYPES or not discounted(p) or p["handle"] not in jpy_by:
            continue
        brand, name = name_parts(p)
        next_id += 1
        e = {"id": f"{next_id:03d}", "brand": brand, "name": name,
             "category": CATEGORY.get(p.get("product_type", ""), "アクセサリー"), "shop": "Linsoul",
             "storeCode": None, "aliCode": None, "purchaseUrl": f"{STORE}/products/{p['handle']}",
             "featured": p["handle"] in keep_featured}
        apply_prices(e, best_variant(p), jpy_by[p["handle"]])
        try:
            img = save_image(p)
        except Exception as ex:
            print(f"画像を取得できませんでした: {p['handle']}: {ex}", file=sys.stderr)
            img = None
        if img:
            e["image"] = img
        data["products"].append(e)
        added += 1
    conf = shop_conf(data)
    for k in ("start", "end", "lastEnded"):
        conf.pop(k, None)
    conf.update({
        "source": "auto",
        "saleName": "セール",
        "detectedAt": TODAY,
        "checkedAt": TODAY,
        "priceLabel": "Linsoulのセール価格（円表示）",
        "notice": "Linsoulの表示価格は、サイトを円表示にしたときのセール価格です。セール期間はLinsoulのサイトでご確認ください。",
        "fineNote": "Linsoulの表示価格は、毎日自動で Linsoul の販売ページを円表示にして確認したセール価格です（Linsoul側で米ドルから換算した金額）。「〜」付きは色・端子などで価格が分かれる商品の最安値です。送料・関税・為替や在庫で支払額は変わります。",
    })
    if "アクセサリー" not in data["campaign"]["categoryOrder"]:
        data["campaign"]["categoryOrder"].append("アクセサリー")
    save_site(html, data)
    body = [f"## Linsoul のセールが始まったようです（{TODAY}）", "", f"- 判断の理由：{report.get('reason')}",
            f"- 下書きに入れた商品：値引き中の {added} 品（円表示・画像つき）", ""]
    if report.get("x_posts"):
        body += ["### Linsoul_JP の X の投稿", ""] + [f"- {x['url']}\n  > {x['text'].replace(chr(10), ' ')}" for x in report["x_posts"]] + [""]
    body += ["### 公開するには", "", "中身を確認して、このプルリクエストを **マージ** してください。数分で schallxd.github.io/sale/ に出ます。",
             "公開しない場合は、マージせずに閉じてください。", "",
             "セール期間が分かったら、index.html の `campaign.shops.Linsoul.end`（例 `2026-11-15T23:59:59+09:00`）に入れると、その時刻で自動的に隠れます。"]
    (ROOT / "data" / "linsoul-pr-body.md").write_text("\n".join(body) + "\n")
    print(f"下書き: {added} 品")


if __name__ == "__main__":
    {"daily": daily, "draft": draft}[sys.argv[1] if len(sys.argv) > 1 else "daily"]()
