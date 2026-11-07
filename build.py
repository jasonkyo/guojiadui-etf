"""汪汪队 ETF 观察：拉数据 → 存快照 → 生成 index.html。由 GitHub Actions 每个交易日收盘后运行（.github/workflows/update.yml）。
数据：腾讯行情（成交额、外盘内盘、场内份额、规模）；上交所每日份额（akshare.fund_etf_scale_sse，补沪市历史）。
深市没有历史份额接口，只靠每天的快照累积。所有结论都是量价推测，不构成投资建议。
"""
import json
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CORE = ["sh510300", "sh510310", "sh510050", "sh510330", "sz159919", "sh512100", "sh588080", "sz159915", "sh510500", "sz159845",
        "sh560010", "sh510180", "sz159629", "sh510230", "sz159922", "sh512500", "sh515800", "sz159952", "sh588050", "sz159901"]
EXT = ["sh588000", "sz159338", "sh512050", "sz159339"]
SSE = [c[2:] for c in CORE + EXT if c.startswith("sh")]
SSE_CORE = [c[2:] for c in CORE if c.startswith("sh")]
STRONG, WARM = 1.8, 1.3
CST = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0"}


def get(url, enc="utf-8"):
    for k in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
                return r.read().decode(enc, errors="ignore")
        except Exception:
            if k == 2:
                raise
            time.sleep(3)


def quotes(codes):
    txt = get("https://qt.gtimg.cn/q=" + ",".join(codes), "gbk")
    rows = []
    for line in txt.strip().split(";"):
        if '"' not in line:
            continue
        f = line.split('"')[1].split("~")
        if len(f) < 77:
            continue
        o, i, amt = float(f[7]), float(f[8]), float(f[37]) * 1e4
        rows.append(dict(code=line.split("=")[0].strip().replace("v_", ""), name=f[1], price=float(f[3]), pct=float(f[32]) / 100,
                         amount=amt, active=(o - i) / (o + i) * amt if o + i > 0 else 0.0, shares=float(f[72] or 0),
                         scale=float(f[44] or 0) * 1e8, date=pd.Timestamp(f[30][:8])))
    return pd.DataFrame(rows)


def hist_amount(code, n=60):
    d = json.loads(get(f"https://web.ifzq.gtimg.cn/appstock/app/kline/kline?param={code},day,,,{n}"))["data"][code]["day"]
    df = pd.DataFrame([r[:6] for r in d], columns=["date", "open", "close", "high", "low", "vol"])
    for c in ["open", "close", "high", "low", "vol"]:
        df[c] = df[c].astype(float)
    df["date"] = pd.to_datetime(df["date"])
    df["amount"] = df.vol * 100 * (df.open + df.close + df.high + df.low) / 4
    return df[["date", "amount"]]


def update_sse(days=12):
    """补最近几天的上交所份额（失败不影响生成页面）"""
    try:
        import akshare as ak
    except Exception as e:
        print("akshare 不可用，跳过上交所份额", e); return
    f = DATA / "sse_shares.csv"
    old = pd.read_csv(f, dtype={"code": str}, parse_dates=["date"]) if f.exists() else pd.DataFrame(columns=["date", "code", "shares"])
    have = set(old["date"])
    rows = []
    today = datetime.now(CST).replace(tzinfo=None)
    for k in range(days, -1, -1):
        d = pd.Timestamp((today - timedelta(days=k)).date())
        if d.weekday() >= 5 or d in have:
            continue
        try:
            t = ak.fund_etf_scale_sse(date=d.strftime("%Y%m%d"))
        except Exception as e:
            print(d.date(), "上交所份额失败", e); continue
        t = t[t["基金代码"].astype(str).isin(SSE)]
        if t.empty:
            continue
        rows += [(d, str(r.基金代码), float(r.基金份额)) for r in t.itertuples()]
        time.sleep(0.5)
    if rows:
        allr = pd.concat([old, pd.DataFrame(rows, columns=["date", "code", "shares"])]).drop_duplicates(["date", "code"], keep="last").sort_values(["date", "code"])
        allr.to_csv(f, index=False, encoding="utf-8-sig")
        print("上交所份额补了", len({r[0] for r in rows}), "天")


def main():
    meta = json.loads((DATA / "meta.json").read_text(encoding="utf-8"))
    q = quotes(CORE + EXT)
    day = q["date"].max()
    # 快照（每天一份，深市份额变化靠它）
    snap_f = DATA / "snapshots.csv"
    sn = pd.read_csv(snap_f, parse_dates=["date"], dtype={"code": str}) if snap_f.exists() else pd.DataFrame()
    prev = sn[sn.date < day] if len(sn) else sn
    snap_new = q[["date", "code", "name", "price", "pct", "amount", "active", "shares", "scale"]]
    pd.concat([sn, snap_new]).drop_duplicates(["date", "code"], keep="last").to_csv(snap_f, index=False, encoding="utf-8-sig")
    update_sse()

    rows = []
    for c in CORE + EXT:
        h = hist_amount(c)
        h = h[h.date <= day]
        prior = h[h.date < day].tail(20)
        r = q[q.code == c].iloc[0]
        code = c[2:]
        rows.append(dict(code=code, full=c, name=r["name"], index=meta.get(code, {}).get("index", ""), hold=meta.get(code, {}).get("hold"),
                         group="core" if c in CORE else "ext", scale=round(r.scale / 1e8, 1), amount=round(r.amount / 1e8, 2),
                         avg20=round(prior.amount.mean() / 1e8, 3) if len(prior) else None, active=round(r.active / 1e8, 2),
                         pct=round(r.pct * 100, 2), shares=round(r.shares / 1e8, 2), price=r.price, dshare=None, dshare_from=None,
                         spark=[round(x / 1e8, 2) for x in h.tail(30).amount]))
        time.sleep(0.2)
    df = pd.DataFrame(rows)
    # 份额较前一天：沪市用上交所历史，深市用快照
    sse = pd.read_csv(DATA / "sse_shares.csv", parse_dates=["date"], dtype={"code": str})
    ds = sorted(sse.date.unique())
    if len(ds) >= 2:
        a0 = sse[sse.date == ds[-1]].set_index("code").shares; b0 = sse[sse.date == ds[-2]].set_index("code").shares
        for i, r in df.iterrows():
            if r.code in a0.index and r.code in b0.index:
                df.at[i, "dshare"] = round((a0[r.code] - b0[r.code]) / 1e8, 2); df.at[i, "dshare_from"] = pd.Timestamp(ds[-2]).strftime("%m-%d")
    if len(prev):
        last = prev[prev.date == prev.date.max()].assign(c6=lambda d: d.code.str[2:]).set_index("c6")
        for i, r in df.iterrows():
            if df.at[i, "dshare"] is None and r.code in last.index:
                df.at[i, "dshare"] = round((r.shares * 1e8 - last.shares[r.code]) / 1e8, 2); df.at[i, "dshare_from"] = prev.date.max().strftime("%m-%d")
    # 沪市核心 13 只的份额历史（图表用）
    s = sse[sse.code.isin(SSE_CORE)]
    tot = s.groupby("date").agg(total=("shares", "sum"), n=("code", "nunique"))
    tot = (tot[tot.n == tot.n.max()].total / 1e8).sort_index()
    if day not in tot.index:
        tot.loc[day] = df[df.code.isin(SSE_CORE)].shares.sum()
    hist = [dict(d=d.strftime("%Y-%m-%d"), total=round(v, 2)) for d, v in tot.sort_index().items()]
    core = df[df.group == "core"]
    tot_amt, avg20 = core.amount.sum(), core.avg20.sum()
    payload = dict(day=day.strftime("%Y-%m-%d"), weekday="一二三四五六日"[day.weekday()], gen=datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
                   strong=STRONG, warm=WARM, rows=json.loads(df.to_json(orient="records", force_ascii=False)), hist=hist,
                   kpi=dict(amount=round(tot_amt, 1), avg20=round(avg20, 1), ratio=round(tot_amt / avg20, 2) if avg20 else None,
                            active=round(core.active.sum(), 2), wpct=round((core.pct * core.scale).sum() / core.scale.sum(), 2)))
    html = (ROOT / "template.html").read_text(encoding="utf-8").replace("__DATA__", json.dumps(payload, ensure_ascii=False))
    (ROOT / "index.html").write_text(html, encoding="utf-8")
    print(f"{day:%Y-%m-%d} 生成完成：成交额 {tot_amt:.1f} 亿，20 日均 {avg20:.1f} 亿，放大 {tot_amt / avg20:.2f} 倍")


if __name__ == "__main__":
    main()
