"""
Chui Public Comparables Platform
Search a listed company by name or ticker, pull its public financials from
Yahoo Finance, SEC EDGAR and Financial Modeling Prep, and build a traceable
EV/Revenue and P/S comparable-company valuation.

Standalone capstone build. Mirrors the 9-tab Comps Engine workbook; intended to
port later into the CRM Public Comparables page.

House style: no long dashes, no asterisks in user-facing copy.
"""

import datetime as dt
import json
import math
import os
import statistics
from typing import Optional

import pandas as pd
import requests
import streamlit as st

try:
    import yfinance as yf
except Exception:  # pragma: no cover
    yf = None

APP_TITLE = "Chui Public Comparables Platform"
UA_DEFAULT = "Chui Ventures Comps Platform contact@chuivc.com"

# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def _m(x) -> Optional[float]:
    """Scale a raw figure to millions, or return None (never 0 for missing)."""
    try:
        if x is None:
            return None
        v = float(x)
        if math.isnan(v):
            return None
        return round(v / 1_000_000, 4)
    except Exception:
        return None


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def ev_from(mktcap_m, debt_m, cash_m, other_m=0.0):
    if mktcap_m is None:
        return None
    ev = mktcap_m
    if debt_m is not None:
        ev += debt_m
    if cash_m is not None:
        ev -= cash_m
    if other_m:
        ev += other_m
    return round(ev, 4)


def ratio(num, den):
    try:
        if num is None or den in (None, 0):
            return None
        if den < 0:
            return None  # NM, not a negative multiple
        return round(num / den, 2)
    except Exception:
        return None


def median_eligible(values):
    vals = [v for v in values if isinstance(v, (int, float)) and v is not None]
    if not vals:
        return None
    return round(statistics.median(vals), 2)


# ----------------------------------------------------------------------------
# Name -> ticker search (Yahoo search endpoint)
# ----------------------------------------------------------------------------

@st.cache_data(ttl=3600, show_spinner=False)
def search_symbols(query: str):
    out = []
    try:
        url = "https://query2.finance.yahoo.com/v1/finance/search"
        r = requests.get(
            url,
            params={"q": query, "quotesCount": 8, "newsCount": 0},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=12,
        )
        if r.ok:
            for q in r.json().get("quotes", []):
                sym = q.get("symbol")
                if not sym:
                    continue
                out.append({
                    "symbol": sym,
                    "name": q.get("shortname") or q.get("longname") or "",
                    "exchange": q.get("exchDisp") or q.get("exchange") or "",
                    "type": q.get("quoteType") or "",
                })
    except Exception:
        pass
    return out


# ----------------------------------------------------------------------------
# Source 1: Yahoo Finance via yfinance
# ----------------------------------------------------------------------------

@st.cache_data(ttl=900, show_spinner=False)
def fetch_yahoo(ticker: str) -> dict:
    o = {
        "source": "Yahoo Finance", "status": "failed",
        "name": None, "price": None, "shares_m": None, "mktcap_m": None,
        "debt_m": None, "cash_m": None, "revenue_ltm_m": None,
        "currency": None, "period_end": None, "retrieved_at": _now_iso(),
        "source_url": f"https://finance.yahoo.com/quote/{ticker}", "notes": "",
    }
    if yf is None:
        o["notes"] = "yfinance not available"
        return o
    try:
        tk = yf.Ticker(ticker)
        fi = getattr(tk, "fast_info", {}) or {}
        o["price"] = fi.get("last_price") or fi.get("lastPrice")
        o["currency"] = fi.get("currency")
        sh = fi.get("shares") or fi.get("shares_outstanding")
        o["shares_m"] = _m(sh) if sh else None
        mc = fi.get("market_cap") or fi.get("marketCap")
        o["mktcap_m"] = _m(mc) if mc else (
            round(o["price"] * o["shares_m"], 4) if (o["price"] and o["shares_m"]) else None
        )
        try:
            info = tk.get_info()
            o["name"] = info.get("shortName") or info.get("longName")
        except Exception:
            pass
        try:
            bs = tk.balance_sheet
            if bs is not None and bs.shape[1] > 0:
                col = bs.columns[0]
                o["period_end"] = str(pd.Timestamp(col).date())

                def pick(names):
                    for n in names:
                        if n in bs.index and pd.notna(bs.loc[n, col]):
                            return bs.loc[n, col]
                    return None

                o["debt_m"] = _m(pick(["Total Debt", "Long Term Debt And Capital Lease Obligation", "Long Term Debt"]))
                o["cash_m"] = _m(pick(["Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"]))
        except Exception as e:
            o["notes"] += f" bs:{e}"
        try:
            qf = tk.quarterly_financials
            if qf is not None and "Total Revenue" in qf.index:
                rev = qf.loc["Total Revenue"].dropna().iloc[:4]
                if len(rev) >= 1:
                    o["revenue_ltm_m"] = _m(rev.sum())
        except Exception as e:
            o["notes"] += f" rev:{e}"
        core = (o["price"] is not None and o["shares_m"] is not None) or o["mktcap_m"] is not None
        o["status"] = "ok" if (core and o["revenue_ltm_m"] is not None) else ("partial" if core else "failed")
    except Exception as e:
        o["notes"] += f" fetch:{e}"
    return o


# ----------------------------------------------------------------------------
# Source 2: Financial Modeling Prep (needs free API key; US coverage)
# ----------------------------------------------------------------------------

@st.cache_data(ttl=900, show_spinner=False)
def fetch_fmp(ticker: str, api_key: str) -> dict:
    o = {
        "source": "Financial Modeling Prep", "status": "failed",
        "name": None, "price": None, "shares_m": None, "mktcap_m": None,
        "debt_m": None, "cash_m": None, "revenue_ltm_m": None,
        "currency": None, "period_end": None, "retrieved_at": _now_iso(),
        "source_url": f"https://financialmodelingprep.com/financial-summary/{ticker}", "notes": "",
    }
    if not api_key:
        o["status"] = "na"
        o["notes"] = "No FMP key provided"
        return o
    # FMP retired the v3 endpoints on 31 Aug 2025. The current free tier exposes the
    # stable profile endpoint (identity, price, market cap); income statement and
    # balance sheet are premium. We use FMP as a market-cap and price cross-check.
    try:
        pr = requests.get("https://financialmodelingprep.com/stable/profile",
                          params={"symbol": ticker, "apikey": api_key}, timeout=12)
        if pr.ok:
            data = pr.json()
            if isinstance(data, list) and data:
                p = data[0]
                o["name"] = p.get("companyName")
                o["price"] = p.get("price")
                o["currency"] = p.get("currency")
                o["mktcap_m"] = _m(p.get("marketCap"))
                o["notes"] = "Free tier: profile only (statements are premium); price and market-cap cross-check"
                o["status"] = "partial" if o["mktcap_m"] is not None else "failed"
            elif isinstance(data, dict):
                o["notes"] = (data.get("Error Message") or str(data))[:140]
            else:
                o["notes"] = "No profile returned"
        else:
            o["notes"] = f"http {pr.status_code}"
    except Exception as e:
        o["notes"] += f" fmp:{e}"
    return o


# ----------------------------------------------------------------------------
# Source 3: SEC EDGAR (free, US filers; official filings)
# ----------------------------------------------------------------------------

@st.cache_data(ttl=86400, show_spinner=False)
def _edgar_ticker_map(ua: str) -> dict:
    try:
        r = requests.get("https://www.sec.gov/files/company_tickers.json",
                         headers={"User-Agent": ua}, timeout=15)
        if r.ok:
            data = r.json()
            return {str(v["ticker"]).upper(): (str(v["cik_str"]).zfill(10), v["title"]) for v in data.values()}
    except Exception:
        pass
    return {}


def _latest_fact(facts, keys):
    """Return (value, period_end) for the most recent USD fact among candidate concepts."""
    best = None
    for k in keys:
        node = facts.get("us-gaap", {}).get(k)
        if not node:
            continue
        for unit, arr in node.get("units", {}).items():
            if not unit.startswith("USD"):
                continue
            for item in arr:
                end = item.get("end")
                val = item.get("val")
                if end is None or val is None:
                    continue
                if best is None or end > best[1]:
                    best = (val, end)
    return best if best else (None, None)


@st.cache_data(ttl=900, show_spinner=False)
def fetch_edgar(ticker: str, ua: str) -> dict:
    o = {
        "source": "SEC EDGAR", "status": "failed",
        "name": None, "price": None, "shares_m": None, "mktcap_m": None,
        "debt_m": None, "cash_m": None, "revenue_ltm_m": None,
        "currency": "USD", "period_end": None, "retrieved_at": _now_iso(),
        "source_url": None, "notes": "",
    }
    tmap = _edgar_ticker_map(ua)
    key = ticker.upper()
    if key not in tmap:
        o["status"] = "na"
        o["notes"] = "Not a US filer in EDGAR (name/ticker not found)"
        return o
    cik, title = tmap[key]
    o["name"] = title
    o["source_url"] = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=10-K"
    try:
        r = requests.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json",
                         headers={"User-Agent": ua}, timeout=20)
        if not r.ok:
            o["notes"] = f"facts http {r.status_code}"
            return o
        facts = r.json().get("facts", {})
        rev, rev_end = _latest_fact(facts, [
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues", "SalesRevenueNet", "RevenueFromContractWithCustomerIncludingAssessedTax",
        ])
        cash, cash_end = _latest_fact(facts, [
            "CashAndCashEquivalentsAtCarryingValue",
            "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        ])
        debt, _ = _latest_fact(facts, [
            "LongTermDebtNoncurrent", "LongTermDebt", "DebtLongtermAndShorttermCombinedAmount",
        ])
        sh, _ = _latest_fact(facts, ["EntityCommonStockSharesOutstanding"]) if "dei" not in facts else (None, None)
        o["revenue_ltm_m"] = _m(rev)
        o["cash_m"] = _m(cash)
        o["debt_m"] = _m(debt)
        o["period_end"] = rev_end or cash_end
        got = sum(x is not None for x in [o["revenue_ltm_m"], o["cash_m"], o["debt_m"]])
        o["status"] = "ok" if got >= 2 else ("partial" if got >= 1 else "failed")
        o["notes"] = "Latest reported filing value; verify period for the valuation date"
    except Exception as e:
        o["notes"] += f" edgar:{e}"
    return o


# ----------------------------------------------------------------------------
# Combine sources into one peer record
# ----------------------------------------------------------------------------

def gather(ticker: str, fmp_key: str, ua: str) -> dict:
    y = fetch_yahoo(ticker)
    f = fetch_fmp(ticker, fmp_key)
    e = fetch_edgar(ticker, ua)
    sources = {"Yahoo Finance": y, "Financial Modeling Prep": f, "SEC EDGAR": e}

    def choose(field, order=("Yahoo Finance", "Financial Modeling Prep", "SEC EDGAR")):
        for s in order:
            v = sources[s].get(field)
            if v is not None:
                return v, s
        return None, None

    name, _ = choose("name")
    price, price_src = choose("price")
    shares, shares_src = choose("shares_m")
    mktcap, mktcap_src = choose("mktcap_m")
    debt, debt_src = choose("debt_m", ("SEC EDGAR", "Yahoo Finance", "Financial Modeling Prep"))
    cash, cash_src = choose("cash_m", ("SEC EDGAR", "Yahoo Finance", "Financial Modeling Prep"))
    rev, rev_src = choose("revenue_ltm_m", ("SEC EDGAR", "Yahoo Finance", "Financial Modeling Prep"))
    currency, _ = choose("currency")

    if mktcap is None and price is not None and shares is not None:
        mktcap = round(price * shares, 4)
        mktcap_src = "computed price x shares"

    ev = ev_from(mktcap, debt, cash)
    rec = {
        "ticker": ticker.upper(), "name": name, "currency": currency or "USD",
        "price": price, "shares_m": shares, "mktcap_m": mktcap,
        "debt_m": debt, "cash_m": cash, "revenue_ltm_m": rev, "ev_m": ev,
        "ev_rev": ratio(ev, rev), "ps": ratio(mktcap, rev),
        "eligible": True, "tier": "Core",
        "src": {"mktcap": mktcap_src, "debt": debt_src, "cash": cash_src, "revenue": rev_src, "price": price_src, "shares": shares_src},
        "raw": sources,
    }
    # Cross-source revenue discrepancy flag (>10% on same basis, best effort)
    revs = [s["revenue_ltm_m"] for s in sources.values() if s.get("revenue_ltm_m")]
    flag = []
    if len(revs) >= 2:
        hi, lo = max(revs), min(revs)
        if hi and abs(hi - lo) / hi > 0.10:
            flag.append(f"Revenue differs across sources by {round(abs(hi - lo) / hi * 100, 1)}%")
    if rev is not None and len([s for s in sources.values() if s.get("revenue_ltm_m")]) == 1:
        flag.append("Single source revenue")
    if currency and currency != "USD":
        flag.append(f"FX {currency}, convert before use")
    rec["flags"] = " | ".join(flag)
    return rec


# ----------------------------------------------------------------------------
# Lami pilot defaults
# ----------------------------------------------------------------------------

LAMI_PEERS = ["GSHD", "TWFG", "AIFU", "POLICYBZR.NS"]
LAMI_INPUTS = {
    "company": "Lami Inc", "sector": "Insurtech, Nairobi",
    "valuation_date": "2026-06-30", "currency": "USD",
    "revenue_ltm_m": 2.853, "cash_m": 0.209, "debt_m": 0.0,
    "ownership_pct": 6.36, "discount_pct": 15.0,
    "instrument": "SAFE (with MFN)", "pro_rata_confirmed": False,
}


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------

st.set_page_config(page_title=APP_TITLE, page_icon="chart", layout="wide")

if "peers" not in st.session_state:
    st.session_state.peers = {}  # ticker -> record
if "pf" not in st.session_state:
    st.session_state.pf = dict(LAMI_INPUTS)

st.sidebar.title("Chui Comps")
st.sidebar.caption("Public comparables valuation platform")
with st.sidebar:
    st.subheader("Settings")
    _default_fmp = ""
    try:
        _default_fmp = st.secrets.get("FMP_API_KEY", "")
    except Exception:
        _default_fmp = os.environ.get("FMP_API_KEY", "")
    fmp_key = st.text_input("Financial Modeling Prep API key", type="password", value=_default_fmp,
                            help="Free key from financialmodelingprep.com. Yahoo and SEC EDGAR work without one.")
    ua = st.text_input("SEC EDGAR contact (User-Agent)", value=UA_DEFAULT,
                       help="SEC asks for a contact email in requests. Any valid contact is fine.")
    st.divider()
    st.caption("Sources: Yahoo Finance, SEC EDGAR, Financial Modeling Prep. "
               "Figures in USD millions. Missing stays missing, never zero.")

st.title(APP_TITLE)
st.caption("Search a listed company, pull its public financials from three free sources, "
           "and build a traceable EV/Revenue and P/S comparables valuation.")

tab_search, tab_comps, tab_val, tab_audit, tab_about = st.tabs(
    ["Search", "Peer set", "Valuation", "Audit trail", "About"]
)

# ---- Search tab ----
with tab_search:
    st.subheader("Find a company by name or ticker")
    c1, c2 = st.columns([3, 1])
    query = c1.text_input("Company name or ticker", placeholder="e.g. Goosehead, or GSHD")
    go = c2.button("Search", use_container_width=True)
    quick = st.columns(5)
    if quick[0].button("Load Lami peer set", use_container_width=True):
        with st.spinner("Pulling the four Lami peers from Yahoo, EDGAR and FMP..."):
            for t in LAMI_PEERS:
                st.session_state.peers[t] = gather(t, fmp_key, ua)
        st.success("Loaded GSHD, TWFG, AIFU and POLICYBZR.NS into the peer set.")

    if query and (go or True):
        hits = search_symbols(query) if not query.isupper() or len(query) > 6 else [{"symbol": query.upper(), "name": "", "exchange": "", "type": "direct"}]
        if not hits:
            hits = [{"symbol": query.upper(), "name": "(direct ticker)", "exchange": "", "type": "direct"}]
        st.write("Matches:")
        for h in hits[:8]:
            cc = st.columns([2, 3, 2, 1])
            cc[0].write(f"**{h['symbol']}**")
            cc[1].write(h.get("name") or "")
            cc[2].write(h.get("exchange") or "")
            if cc[3].button("Pull", key=f"pull_{h['symbol']}"):
                with st.spinner(f"Pulling {h['symbol']} from all sources..."):
                    rec = gather(h["symbol"], fmp_key, ua)
                    st.session_state.peers[h["symbol"]] = rec
                st.success(f"Added {h['symbol']} to the peer set.")
                r = st.session_state.peers[h["symbol"]]
                m = st.columns(5)
                m[0].metric("Market cap (m)", r["mktcap_m"] if r["mktcap_m"] is not None else "Missing")
                m[1].metric("EV (m)", r["ev_m"] if r["ev_m"] is not None else "Missing")
                m[2].metric("LTM rev (m)", r["revenue_ltm_m"] if r["revenue_ltm_m"] is not None else "Missing")
                m[3].metric("EV/Revenue", f"{r['ev_rev']}x" if r["ev_rev"] is not None else "NM")
                m[4].metric("P/S", f"{r['ps']}x" if r["ps"] is not None else "NM")
                # per-source comparison
                comp = []
                for sname, s in r["raw"].items():
                    comp.append({
                        "Source": sname, "Status": s["status"],
                        "Mkt cap (m)": s["mktcap_m"], "Debt (m)": s["debt_m"],
                        "Cash (m)": s["cash_m"], "LTM rev (m)": s["revenue_ltm_m"],
                        "Period end": s["period_end"],
                    })
                st.dataframe(pd.DataFrame(comp), use_container_width=True, hide_index=True)
                if r["flags"]:
                    st.warning(r["flags"])

# ---- Peer set tab ----
with tab_comps:
    st.subheader("Approved peer set")
    if not st.session_state.peers:
        st.info("No peers yet. Use the Search tab, or load the Lami peer set.")
    else:
        rows = []
        for t, r in st.session_state.peers.items():
            rows.append({
                "Eligible": r["eligible"], "Ticker": t, "Company": r["name"],
                "Mkt cap (m)": r["mktcap_m"], "EV (m)": r["ev_m"],
                "LTM rev (m)": r["revenue_ltm_m"], "EV/Rev": r["ev_rev"], "P/S": r["ps"],
                "Flags": r["flags"],
            })
        df = pd.DataFrame(rows)
        edited = st.data_editor(
            df, use_container_width=True, hide_index=True,
            column_config={
                "Eligible": st.column_config.CheckboxColumn(help="Untick to exclude a peer from the median"),
                "EV/Rev": st.column_config.NumberColumn(format="%.2f"),
                "P/S": st.column_config.NumberColumn(format="%.2f"),
            },
            disabled=["Ticker", "Company", "Mkt cap (m)", "EV (m)", "LTM rev (m)", "EV/Rev", "P/S", "Flags"],
            key="peer_editor",
        )
        # push eligibility edits back
        for _, row in edited.iterrows():
            if row["Ticker"] in st.session_state.peers:
                st.session_state.peers[row["Ticker"]]["eligible"] = bool(row["Eligible"])

        elig_evrev = [r["ev_rev"] for r in st.session_state.peers.values() if r["eligible"] and r["ev_rev"] is not None]
        elig_ps = [r["ps"] for r in st.session_state.peers.values() if r["eligible"] and r["ps"] is not None]
        m = st.columns(4)
        m[0].metric("Eligible peers", len(elig_evrev))
        m[1].metric("Median EV/Revenue", f"{median_eligible(elig_evrev)}x" if elig_evrev else "No usable peers")
        m[2].metric("Median P/S", f"{median_eligible(elig_ps)}x" if elig_ps else "No usable peers")
        if len(elig_evrev) < 3:
            st.warning("Fewer than three eligible peers. Requires Augustine's approval before use.")
        cc = st.columns(2)
        if cc[0].button("Refresh all peers"):
            with st.spinner("Refreshing..."):
                for t in list(st.session_state.peers):
                    elig = st.session_state.peers[t]["eligible"]
                    st.session_state.peers[t] = gather(t, fmp_key, ua)
                    st.session_state.peers[t]["eligible"] = elig
            st.success("Refreshed.")
        csv = pd.DataFrame(rows).to_csv(index=False).encode()
        cc[1].download_button("Download peer table (CSV)", csv, "comps_peers.csv", "text/csv")

# ---- Valuation tab ----
with tab_val:
    st.subheader("Portfolio company valuation")
    pf = st.session_state.pf
    c1, c2, c3 = st.columns(3)
    pf["company"] = c1.text_input("Portfolio company", pf["company"])
    pf["valuation_date"] = c2.text_input("Valuation date", pf["valuation_date"])
    pf["currency"] = c3.text_input("Reporting currency", pf["currency"])
    c1, c2, c3, c4 = st.columns(4)
    pf["revenue_ltm_m"] = c1.number_input("LTM net revenue (m)", value=float(pf["revenue_ltm_m"]), step=0.01, format="%.3f")
    pf["cash_m"] = c2.number_input("Eligible cash (m)", value=float(pf["cash_m"]), step=0.01, format="%.3f")
    pf["debt_m"] = c3.number_input("Debt (m)", value=float(pf["debt_m"]), step=0.01, format="%.3f")
    pf["ownership_pct"] = c4.number_input("Chui ownership FD (%)", value=float(pf["ownership_pct"]), step=0.01, format="%.2f")
    c1, c2, c3 = st.columns(3)
    method = c1.selectbox("Method", ["EV/Revenue", "P/S"])
    pf["discount_pct"] = c2.number_input("Approved adjustment / discount (%)", value=float(pf["discount_pct"]), step=0.5, format="%.1f")
    pf["pro_rata_confirmed"] = c3.checkbox("Pro-rata holding confirmed (Augustine)", value=pf["pro_rata_confirmed"])

    elig_evrev = [r["ev_rev"] for r in st.session_state.peers.values() if r["eligible"] and r["ev_rev"] is not None]
    elig_ps = [r["ps"] for r in st.session_state.peers.values() if r["eligible"] and r["ps"] is not None]
    med_evrev = median_eligible(elig_evrev)
    med_ps = median_eligible(elig_ps)

    st.divider()
    if method == "EV/Revenue":
        base = med_evrev
        if base is None:
            st.warning("No eligible EV/Revenue peers. Load or include peers first.")
        else:
            selected = round(base * (1 - pf["discount_pct"] / 100), 4)
            implied_ev = round(selected * pf["revenue_ltm_m"], 4)
            equity = round(implied_ev + pf["cash_m"] - pf["debt_m"], 4)
            undiscounted = round(base * pf["revenue_ltm_m"] + pf["cash_m"] - pf["debt_m"], 4)
            m = st.columns(5)
            m[0].metric("Peer median EV/Rev", f"{base}x")
            m[1].metric("Selected multiple", f"{selected}x", help=f"{base}x less {pf['discount_pct']}%")
            m[2].metric("Implied EV (m)", implied_ev)
            m[3].metric("Equity value (m)", equity)
            m[4].metric("Undiscounted ref (m)", undiscounted)
            if pf["pro_rata_confirmed"]:
                st.metric("Chui holding value (m)", round(equity * pf["ownership_pct"] / 100, 4))
            else:
                st.info("Holding value requires finance review (instrument is a SAFE; pro-rata not confirmed).")
            st.caption(f"At {pf['valuation_date']}, {len(elig_evrev)} eligible core peers had a median EV/Revenue of "
                       f"{base}x. The approved {pf['discount_pct']}% adjustment gives a selected multiple of {selected}x. "
                       f"Applied to LTM revenue of {pf['revenue_ltm_m']}m, this implies EV of {implied_ev}m and equity "
                       f"value of {equity}m. Model-implied only; becomes a mark after Augustine's review.")
    else:
        base = med_ps
        if base is None:
            st.warning("No eligible P/S peers.")
        else:
            selected = round(base * (1 - pf["discount_pct"] / 100), 4)
            equity = round(selected * pf["revenue_ltm_m"], 4)  # P/S gives equity directly
            m = st.columns(4)
            m[0].metric("Peer median P/S", f"{base}x")
            m[1].metric("Selected multiple", f"{selected}x")
            m[2].metric("Equity value (m)", equity)
            if pf["pro_rata_confirmed"]:
                m[3].metric("Chui holding value (m)", round(equity * pf["ownership_pct"] / 100, 4))
            st.caption("P/S gives equity value directly. No EV-to-equity bridge is applied, and P/S is never "
                       "averaged with EV/Revenue.")

# ---- Audit tab ----
with tab_audit:
    st.subheader("Raw observations and source audit trail")
    if not st.session_state.peers:
        st.info("No data yet.")
    else:
        rows = []
        for t, r in st.session_state.peers.items():
            for sname, s in r["raw"].items():
                rows.append({
                    "Ticker": t, "Source": sname, "Status": s["status"],
                    "Mkt cap (m)": s["mktcap_m"], "Debt (m)": s["debt_m"], "Cash (m)": s["cash_m"],
                    "LTM rev (m)": s["revenue_ltm_m"], "Currency": s["currency"],
                    "Period end": s["period_end"], "Retrieved at": s["retrieved_at"],
                    "Source URL": s["source_url"], "Notes": s["notes"],
                })
        adf = pd.DataFrame(rows)
        st.dataframe(adf, use_container_width=True, hide_index=True)
        st.download_button("Download audit trail (CSV)", adf.to_csv(index=False).encode(),
                           "comps_audit_trail.csv", "text/csv")

# ---- About tab ----
with tab_about:
    st.markdown(
        """
### What this is
A standalone public-comparables platform for Chui Ventures. Search a listed company by
name or ticker, pull its financials live from three free sources, and build a traceable
EV/Revenue and P/S valuation for a portfolio company.

### Sources
- **Yahoo Finance** for price, shares, balance sheet and quarterly revenue.
- **SEC EDGAR** for official US-filing financials (free, no key), used as a second source.
- **Financial Modeling Prep** for company profile, price and market cap (free key). Income
  statement and balance sheet are a paid tier, so FMP is used here as a price and market-cap
  cross-check rather than a statements source.

### Finance rules it keeps
- EV/Revenue is an enterprise multiple; P/S is an equity multiple. They are never mixed or averaged.
- Market cap = price times shares. EV = market cap + debt - cash.
- Median uses eligible core peers only. Fewer than three needs reviewer approval.
- Missing data stays missing, never zero. Non-USD is flagged for conversion.
- The model output is a draft; it becomes an approved mark only after Augustine reviews it.

### Relationship to the Comps Engine workbook
This platform is the search-and-pull front for the 9-tab Comps Engine Google Sheet. The
workbook remains the audited valuation record; this app makes finding and pulling peers fast
and is intended to port later into the CRM Public Comparables page.
"""
    )
