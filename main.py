"""
美股热点扫描 - S&P 500 成交额Top100 行业强度分析
基于新浪财经 API + GitHub Actions + pushplus 推送

核心逻辑：
  候选池 = S&P 500 + NASDAQ 100（~600只）
  → 新浪 API 批量获取实时行情，按成交额排序取 Top100
  → 按 GICS 行业分类统计强度得分 S = (w/n) × ln(1+n)
  → 取 Top5 行业，每个行业按成交额取 Top7 个股推送

数据源：新浪财经 hq.sinajs.cn（免费、国内可用、批量查询）
行业分类：Wikipedia S&P 500 获取 GICS 分类
"""
import time
import json
import math
import re
import os
import urllib.request
from datetime import datetime
from collections import defaultdict

# ============================================================
# 配置
# ============================================================
MAIN_BOARD_TOP_N = 100
SUB_BOARD_TOP_N = 7
TOP_INDUSTRIES = 5
AMOUNT_UNIT = 1e8
BATCH_SIZE = 200  # 新浪API单次查询上限（URL长度限制）

PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "b6ff4f2c9949413690b7f9572acdd2a8")
PUSHPLUS_URL = "http://www.pushplus.plus/send"

SINA_URL = "https://hq.sinajs.cn/list="
SINA_HEADERS = {"Referer": "https://finance.sina.com.cn"}
WATCHLIST_FILE = "watchlist.json"

# ============================================================
# 股票池
# ============================================================

def build_watchlist():
    """构建候选池：S&P 500（含行业）+ NASDAQ 100"""
    watchlist = {}

    # S&P 500（含行业分类）
    print("获取 S&P 500...")
    sp500 = _fetch_sp500()
    for item in sp500:
        watchlist[item["ticker"]] = item
    print(f"  S&P 500: {len(sp500)} 只")

    # NASDAQ 100
    print("获取 NASDAQ 100...")
    nasdaq100 = _fetch_nasdaq100()
    for item in nasdaq100:
        if item["ticker"] not in watchlist:
            watchlist[item["ticker"]] = item
    print(f"  NASDAQ 100 新增: {len(watchlist) - len(sp500)} 只")

    watchlist = {k: v for k, v in watchlist.items() if re.match(r'^[A-Z]{1,5}$', k)}
    print(f"总计: {len(watchlist)} 只")

    with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(watchlist, f, ensure_ascii=False)
    return watchlist


def _fetch_sp500():
    """Wikipedia S&P 500（含 GICS sector + industry）"""
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8")
    except Exception as e:
        print(f"    Wikipedia error: {e}")
        return []

    table_match = re.search(r'<table[^>]*wikitable[^>]*>(.*?)</table>', html, re.DOTALL)
    if not table_match:
        print("    Wikipedia: 未找到表格")
        return []

    rows = re.findall(r'<tr>(.*?)</tr>', table_match.group(1), re.DOTALL)
    results = []
    for row_html in rows:
        cells = re.findall(r'<(?:td|th)[^>]*>(.*?)</(?:td|th)>', row_html, re.DOTALL)
        if len(cells) < 4:
            continue
        ticker = re.sub(r'<[^>]+>', '', cells[0]).strip().replace(".", "-")
        name = re.sub(r'<[^>]+>', '', cells[1]).strip()
        sector = re.sub(r'<[^>]+>', '', cells[2]).strip()   # col 2 = GICS Sector
        industry = re.sub(r'<[^>]+>', '', cells[3]).strip() # col 3 = GICS Sub-Industry
        if ticker and re.match(r'^[A-Z]{1,5}$', ticker) and ticker != "Symbol":
            results.append({"ticker": ticker, "name": name, "sector": sector or "Unknown", "industry": industry or "Unknown"})
    return results


def _fetch_nasdaq100():
    """NASDAQ API 获取 NASDAQ 100"""
    url = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=110&exchange=nasdaq&marketcap=mega%7Clarge"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except Exception:
        return []
    rows = data.get("data", {}).get("table", {}).get("rows", [])
    return [{"ticker": r["symbol"], "name": r.get("name", r["symbol"]), "sector": "Unknown", "industry": "Unknown"}
            for r in rows if re.match(r'^[A-Z]{1,5}$', r.get("symbol", ""))]


# ============================================================
# 行情获取（新浪财经）
# ============================================================

def fetch_sina_batch(symbols):
    """批量获取新浪美股行情
    返回: {ticker: {name, price, prev_close, volume, change_pct}}
    """
    codes = ",".join(f"gb_{s.lower()}" for s in symbols)
    url = SINA_URL + codes
    req = urllib.request.Request(url, headers=SINA_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("gbk")
    except Exception as e:
        print(f"  sina fetch error: {e}")
        return {}

    results = {}
    for line in raw.strip().split("\n"):
        parts = line.split('"')
        if len(parts) < 2:
            continue
        ticker = parts[0].replace("var hq_str_gb_", "").replace("=", "").upper()
        fields = parts[1].split(",")
        if len(fields) < 11:
            continue
        try:
            price = float(fields[1])
            prev = float(fields[5])
            volume = int(fields[10])
        except (ValueError, IndexError):
            continue
        if price <= 0 or prev <= 0:
            continue
        # 涨跌幅自己算（新浪 fields[4] 是涨跌额不是涨跌幅）
        change_pct = (price - prev) / prev * 100
        results[ticker] = {
            "price": price,
            "prev_close": prev,
            "volume": volume,
            "change_pct": change_pct,
        }
    return results


# ============================================================
# 核心策略
# ============================================================

def get_top100(watchlist):
    """新浪批量获取行情，按成交额排序取 Top100"""
    symbols = list(watchlist.keys())
    all_data = {}

    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        batch_data = fetch_sina_batch(batch)
        all_data.update(batch_data)
        print(f"  进度: {min(i+BATCH_SIZE, len(symbols))}/{len(symbols)}, 获取: {len(batch_data)}")

    # 合并行业信息，计算成交额
    results = []
    for ticker, quote in all_data.items():
        info = watchlist.get(ticker, {})
        amount = quote["volume"] * quote["price"]
        results.append({
            "ticker": ticker,
            "name": info.get("name", quote["name"]),
            "sector": info.get("sector", "Unknown"),
            "industry": info.get("industry", "Unknown"),
            "price": round(quote["price"], 2),
            "prev_close": round(quote["prev_close"], 2),
            "amount": amount,
            "amount_yi": round(amount / AMOUNT_UNIT, 2),
            "change_pct": round(quote["change_pct"], 2),
        })

    results.sort(key=lambda x: x["amount"], reverse=True)
    return results[:MAIN_BOARD_TOP_N]


def analyze_industries(stocks):
    """行业强度：S = (w/n) × ln(1+n)"""
    if not stocks:
        return []

    groups = defaultdict(list)
    for s in stocks:
        ind = s["industry"] if s["industry"] != "Unknown" else s["sector"]
        if ind == "Unknown":
            ind = "其他"
        groups[ind].append(s)

    scores = []
    for ind_name, members in groups.items():
        n = len(members)
        up = sum(1 for s in members if s["change_pct"] > 0)
        down = sum(1 for s in members if s["change_pct"] < 0)
        direction = "up" if up >= down else "down"
        dominant = up if direction == "up" else down
        win_rate = dominant / n if n > 0 else 0
        score = win_rate * math.log(1 + n)
        scores.append({"name": ind_name, "direction": direction, "total": n, "up": up, "down": down, "score": round(score, 3), "stocks": members})

    scores.sort(key=lambda x: x["score"], reverse=True)
    return scores[:TOP_INDUSTRIES]


# ============================================================
# 推送
# ============================================================

def format_html(top_industries, time_str):
    if not top_industries:
        return None
    html = f"<h3>美股热点扫描 {time_str}</h3>"
    for ind in top_industries:
        is_up = ind["direction"] == "up"
        color = "#ff4444" if is_up else "#00aa00"
        arrow = "↑" if is_up else "↓"
        text = "强势上涨" if is_up else "弱势下跌"
        html += "<hr>"
        html += f'<p><b>■ {ind["name"]}</b> <span style="color:{color};font-weight:bold;">[{arrow}{text}]</span> ｜ 共{ind["total"]}只(涨{ind["up"]}/跌{ind["down"]})</p>'
        top7 = sorted(ind["stocks"], key=lambda x: x["amount"], reverse=True)[:SUB_BOARD_TOP_N]
        if top7:
            html += '<table border="1" cellpadding="3" cellspacing="0" style="font-size:12px;border-collapse:collapse;">'
            for s in top7:
                pc = "#ff4444" if s["change_pct"] > 0 else ("#00aa00" if s["change_pct"] < 0 else "#666")
                html += f'<tr><td>{s["ticker"]}</td><td>{s["name"]}</td><td>{s["amount_yi"]:.2f}亿$</td><td style="color:{pc};font-weight:bold;">{s["change_pct"]:+.2f}%</td></tr>'
            html += "</table>"
    return html


def send_pushplus(date_str, time_str, html_content):
    data = {"token": PUSHPLUS_TOKEN, "title": f"美股热点扫描 {date_str} {time_str}", "content": html_content, "template": "html"}
    req = urllib.request.Request(PUSHPLUS_URL, data=json.dumps(data).encode("utf-8"), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            if result.get("code") == 200:
                print(f"pushplus OK: {time_str}")
            else:
                print(f"pushplus FAIL: {result}")
    except Exception as e:
        print(f"pushplus ERROR: {e}")


# ============================================================
# 主流程
# ============================================================

def main():
    start = time.time()
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M")
    print(f"=== 美股热点扫描 [{time_str}] ===")

    # 1. 股票池
    if os.path.exists(WATCHLIST_FILE):
        with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
            watchlist = json.load(f)
        print(f"加载缓存股票池: {len(watchlist)} 只")
    else:
        watchlist = build_watchlist()

    if not watchlist:
        print("股票池为空")
        return

    # 2. 获取行情
    print("获取行情...")
    top100 = get_top100(watchlist)
    if not top100:
        print("无有效数据")
        return
    print(f"Top100: {top100[0]['ticker']} {top100[0]['amount_yi']:.2f}亿 ~ {top100[-1]['ticker']} {top100[-1]['amount_yi']:.2f}亿")

    # 3. 行业分析
    print("行业分析...")
    top_industries = analyze_industries(top100)
    if not top_industries:
        print("无强势行业")
        return
    for i, ind in enumerate(top_industries, 1):
        d = "↑" if ind["direction"] == "up" else "↓"
        print(f"  {i}. {ind['name']} {d} {ind['total']}只(涨{ind['up']}/跌{ind['down']}) S={ind['score']}")

    # 4. 推送
    html = format_html(top_industries, time_str)
    if html:
        send_pushplus(date_str, time_str, html)

    print(f"=== 完成 耗时 {time.time()-start:.1f}s ===")


if __name__ == "__main__":
    main()
