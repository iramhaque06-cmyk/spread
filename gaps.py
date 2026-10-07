"""The gap between venues, and the honesty rules around it.

A gap is **max minus min of fresh real-money prices**, in points, and it only
exists when at least two fresh real-money venues quote the same event. Those
rules are enforced here rather than in a renderer, so no page can quietly
widen them:

* **Real money only.** ``prediction_market`` and de-vigged ``sportsbook``
  prices count. Mana (``play_money``) and community forecasts are shown
  beside them and never enter the arithmetic - a play-money price is not a
  price anyone can trade against.
* **Fresh only.** A price that cannot be shown to be from the last 24 hours is
  excluded (:mod:`freshness` decides, per venue, from the best evidence the
  venue publishes). One fresh venue is not a gap; it is a single number.
* **Possible matches never count.** A pair the matcher scored below 0.85 is
  stored as "possible" and is excluded here, so a near-match cannot manufacture
  a spread that does not exist.

``rules_differ`` is deliberately *not* a reason to hide a gap: two markets can
settle differently and still disagree on price, and that disagreement is the
interesting thing - it is shown with the caveat attached.
"""

from __future__ import annotations

import freshness

REAL_MONEY = frozenset({"prediction_market", "sportsbook"})


def member_price(row: dict, outcome: str = "") -> float | None:
    """The comparable price for one member of a matched event.

    Prediction markets quote the YES side in ``price``. A sportsbook event has
    no YES side, so the de-vigged probability of the named outcome is used -
    which is why the member carries an outcome name.
    """
    if outcome:
        for item in row.get("outcomes") or []:
            if str(item.get("name", "")).strip().lower() == outcome.strip().lower():
                price = item.get("price")
                return None if price is None else float(price)
        return None
    price = row.get("price")
    return None if price is None else float(price)


def event_prices(members, markets_by_key, *, now=None) -> list[dict]:
    """One comparable, freshness-labelled price per matched member."""
    points = []
    for member in members:
        key = (member.get("venue"), member.get("market_id"))
        row = markets_by_key.get(key)
        if row is None:
            continue
        price = member_price(row, member.get("outcome") or "")
        if price is None:
            continue
        verdict = freshness.assess(row, now=now)
        venue_type = row.get("venue_type") or ""
        points.append(
            {
                "venue": row.get("venue"),
                "market_id": row.get("market_id"),
                "venue_type": venue_type,
                "price": round(price, 2),
                "fresh": bool(verdict.fresh),
                "basis": verdict.basis,
                "reason": verdict.reason,
                "real_money": venue_type in REAL_MONEY,
                "gap_eligible": bool(verdict.fresh) and venue_type in REAL_MONEY,
            }
        )
    return points


def gap(points) -> float | None:
    """Max minus min of the fresh real-money prices, or ``None``."""
    eligible = [point["price"] for point in points if point.get("gap_eligible")]
    if len(eligible) < 2:
        return None
    return round(max(eligible) - min(eligible), 2)


def summarise(points) -> dict:
    """The gap plus the count it was computed from, for the export."""
    return {
        "gap": gap(points),
        "real_money": sum(1 for point in points if point.get("real_money")),
        "fresh": sum(1 for point in points if point.get("fresh")),
        "venues": sorted({point["venue"] for point in points if point.get("venue")}),
    }
