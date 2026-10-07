# Chui Public Comparables Platform

A standalone web app for Chui Ventures. Search a listed company by name or ticker,
pull its public financials live from three free sources, and build a traceable
EV/Revenue and P/S comparable-company valuation for a portfolio company.

It is the search-and-pull front for the 9-tab Comps Engine Google Sheet, and is
intended to port later into the CRM Public Comparables page.

## Sources
- Yahoo Finance (price, shares, balance sheet, quarterly revenue)
- SEC EDGAR (official US filing financials, free, no key) used as a second source
- Financial Modeling Prep (company profile, price, market cap; free key. Statements are a
  paid tier, so FMP is used as a price and market-cap cross-check)

## Run it locally
```bash
cd Comps-Platform
python3 -m pip install -r requirements.txt
streamlit run app.py
```
It opens at http://localhost:8501.

## Keys
- Yahoo and SEC EDGAR need no key.
- Financial Modeling Prep: get a free key at financialmodelingprep.com, paste it in
  the sidebar. Without it the app still works on Yahoo plus EDGAR.
- SEC EDGAR asks for a contact email in the request User-Agent; a default is set in
  the sidebar and can be changed.

## How to use
1. Open the app, enter the FMP key in the sidebar (optional).
2. Search tab: type a company name or ticker, then Pull, or click Load sample peer set.
3. Peer set tab: review the comparable table, untick a peer to exclude it, read the median.
4. Valuation tab: enter the portfolio company inputs and the approved discount; read the
   implied EV and equity value. Holding value stays blocked until pro-rata is confirmed.
5. Audit trail tab: every observation with its source, period and retrieval time; download CSV.

## Finance rules it enforces
- EV/Revenue is an enterprise multiple; P/S is an equity multiple. Never mixed or averaged.
- Market cap = price times shares. EV = market cap + debt - cash.
- Median uses eligible core peers only; fewer than three needs reviewer approval.
- Missing data stays missing, never zero. Non-USD is flagged for conversion.
- Model output is a draft; it becomes a mark only after the finance reviewer approves it.

## Notes
- No credentials are stored in the code. The FMP key is entered at runtime.
- This app supports the valuation; it does not decide a final reporting value.
