# Spread prototype
Goal: pull prices for the same real-world event from two prediction-market
venues and show where they disagree.
anything with the (FOR NOW) could be changed later on

Scope:
- Two venues, public read-only APIs only. No logins, no trading, no money. (FOR NOW)
- One category: economic data events (e.g. Fed decisions).
- SQLite storage (spread.db). Output: static index.html table with both
  prices, the gap, volume, links to each market, last-updated time.
- Every number shows its source venue plus a caveat: fees, liquidity and
  resolution rules may differ.
- Informational only. No advice or profit language (FOR NOW). 18+ notice on the page. 
Out of scope: accounts, payments, alerts, anything involving real money.