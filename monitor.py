#!/usr/bin/env python3
"""Monitor Saudia & Etihad flight prices on offers.reward360.in for BOM→YYZ round trip.

Tracks cheapest + fastest options per airline with ±DATE_FLEX day flexibility.
Designed to run in GitHub Actions on a cron schedule.
Sends Telegram notification when prices change.
"""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

# ---------- Configuration ----------
ORIGIN = "BOM"
DEST = "YYZ"
DEPARTURE = "2026-07-28"
RETURN = "2026-09-27"
DATE_FLEX = 7  # ± days to check around preferred dates
ADULTS = 2
CHILD = 0
INFANTS = 0
PASSENGERS_LABEL = f"{ADULTS} Adult" + (f", {CHILD} Child" if CHILD else "") + (f", {INFANTS} Infant" if INFANTS else "")
TRAVEL_CLASS = "Economy"
MIN_PRICE = 180000
MAX_PRICE = 240000
STATE_FILE = "prices.json"
SEARCH_URL = "https://offers.reward360.in/api/flightSearch"
TARGET_AIRLINES = {"Saudia", "Etihad Airways"}

# Airline-specific flight filters — only track combinations with these flight numbers
AIRLINE_FLIGHT_FILTERS = {
    "Etihad Airways": {
        "outbound": ["EY205"],  # BOM→AUH
        "inbound": ["EY22"],    # YYZ→AUH
    },
}

# Telegram — set via env vars (GitHub secrets)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
# -----------------------------------


def fmt_duration(mins):
    h = mins // 60
    m = mins % 60
    if h == 0:
        return f"{m}m"
    return f"{h}h {m}m"


def date_list(base_str, delta):
    base_dt = datetime.strptime(base_str, "%Y-%m-%d")
    return [(base_dt + timedelta(days=d)).strftime("%Y-%m-%d") for d in range(-delta, delta + 1)]


def has_flight_code(segments, seg_type, codes):
    """Check if any segment of the given type has legs matching one of the flight codes."""
    for seg in segments:
        if seg.get("type") != seg_type:
            continue
        for leg in seg.get("legs", []):
            leg_code = f"{leg.get('airlineCode', '')}{leg.get('flightNumber', '')}"
            if leg_code in codes:
                return True
    return False


def passes_flight_filter(airline, segments):
    filters = AIRLINE_FLIGHT_FILTERS.get(airline)
    if not filters:
        return True
    out_ok = has_flight_code(segments, "OUTBOUND", filters.get("outbound", []))
    in_ok = has_flight_code(segments, "INBOUND", filters.get("inbound", []))
    return out_ok and in_ok


def parse_api_response(result_stdout):
    data = json.loads(result_stdout)
    if not data.get("success"):
        return {}

    journeys = data["data"]["journeys"]
    results = {}

    for j in journeys:
        for seg in j.get("journeyDetails", []):
            outbound_legs = []
            inbound_legs = []
            for ls in seg.get("journeySegments", []):
                for leg in ls.get("legs", []):
                    if ls.get("type") == "OUTBOUND":
                        outbound_legs.append(leg)
                    else:
                        inbound_legs.append(leg)

            airlines = set()
            for leg in outbound_legs + inbound_legs:
                an = leg.get("airlineName", "")
                if an:
                    airlines.add(an)

            matched = airlines & TARGET_AIRLINES
            if not matched:
                continue

            # Apply per-airline flight filters
            matched = {
                a for a in matched
                if passes_flight_filter(a, seg.get("journeySegments", []))
            }
            if not matched:
                continue

            out_dur = sum(leg.get("durationInMins", 0) for leg in outbound_legs)
            in_dur = sum(leg.get("durationInMins", 0) for leg in inbound_legs)
            total_dur = out_dur + in_dur

            outbound_str = ""
            inbound_str = ""
            if outbound_legs:
                outbound_str = f"{outbound_legs[0]['departureDateTime']} → {outbound_legs[-1]['arrivalDateTime']}  ✈ {fmt_duration(out_dur)}"
            if inbound_legs:
                inbound_str = f"{inbound_legs[0]['departureDateTime']} → {inbound_legs[-1]['arrivalDateTime']}  ✈ {fmt_duration(in_dur)}"

            for ps in seg.get("PriceSummaries", []):
                total = ps.get("totalFare", 0)
                if not total:
                    continue

                entry = {
                    "airline": list(matched)[0],
                    "total_fare": total,
                    "base_fare": ps.get("baseFare", 0),
                    "partner": ps.get("partnerName", "Unknown"),
                    "outbound_stops": max(0, len(outbound_legs) - 1),
                    "inbound_stops": max(0, len(inbound_legs) - 1),
                    "outbound": outbound_str,
                    "inbound": inbound_str,
                    "outbound_duration_mins": out_dur,
                    "inbound_duration_mins": in_dur,
                    "total_duration_mins": total_dur,
                }

                airline_key = list(matched)[0]
                if airline_key not in results:
                    results[airline_key] = []
                results[airline_key].append(entry)

    output = {}
    for airline, entries in results.items():
        cheapest = min(entries, key=lambda e: e["total_fare"])
        fastest = min(entries, key=lambda e: e["total_duration_mins"])
        output[airline] = {
            "cheapest": cheapest,
            "fastest": fastest,
            "all": entries,
        }

    return output


def fetch_prices_for_date(departure_date, return_date, attempt=1):
    ts = int(datetime.now().timestamp())
    body = json.dumps({
        "flightfrom": ORIGIN, "flightto": DEST,
        "fromCity": "Mumbai", "toCity": "Toronto",
        "fromContry": "IN", "fromCountryFullName": "India",
        "toCountryFullName": "Canada", "toContry": "CA",
        "fromAirportName": "Chatrapati Shivaji Airport",
        "toAirportName": "Lester B Pearson",
        "flightclass": "E", "flightdefault": "R",
        "departure": departure_date, "arrival": return_date,
        "adults": str(ADULTS), "child": str(CHILD), "infants": str(INFANTS),
        "mobFromAddress": "", "mobToAddress": "",
        "benchmark": "1", "travel": "INT", "flightfaretype": "",
        "traceId": f"mon_{ts}", "timestamp": ts,
    })
    user_agents = [
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    ]
    ua = user_agents[(ts % len(user_agents))]
    result = subprocess.run(
        ["curl", "-s", "--max-time", "25", SEARCH_URL,
         "-H", "Content-Type: application/json",
         "-H", f"User-Agent: {ua}",
         "-H", "Referer: https://offers.reward360.in/v2/compare-fly",
         "-H", "Origin: https://offers.reward360.in",
         "-d", body],
        capture_output=True, text=True, timeout=30)

    if result.returncode != 0:
        raise RuntimeError(f"curl failed (exit {result.returncode}): {result.stderr}")

    # Try to parse — if empty/non-JSON, retry once
    try:
        return parse_api_response(result.stdout)
    except (json.JSONDecodeError, KeyError) as e:
        if attempt < 2:
            print(f"  [API retry {attempt}: bad response ({e}), retrying...]")
            return fetch_prices_for_date(departure_date, return_date, attempt=2)
        # Show what we got for debugging
        preview = result.stdout[:500] if result.stdout else "(empty)"
        print(f"  [API response preview: {preview}]")
        raise


def fetch_prices():
    return fetch_prices_for_date(DEPARTURE, RETURN)


def fetch_flex_prices():
    dep_dates = date_list(DEPARTURE, DATE_FLEX)
    ret_dates = date_list(RETURN, DATE_FLEX)
    base_results = fetch_prices_for_date(DEPARTURE, RETURN)

    dep_by_airline = {}
    fetch_dep = [d for d in dep_dates if d != DEPARTURE]
    with ThreadPoolExecutor(max_workers=5) as ex:
        future_map = {ex.submit(fetch_prices_for_date, d, RETURN): d for d in fetch_dep}
        for f in as_completed(future_map):
            d = future_map[f]
            try:
                results = f.result()
                for airline, info in results.items():
                    c = info["cheapest"]
                    dep_by_airline.setdefault(airline, []).append(
                        (d, c["total_fare"], c["partner"], c["total_duration_mins"]))
            except Exception:
                pass

    ret_by_airline = {}
    fetch_ret = [r for r in ret_dates if r != RETURN]
    with ThreadPoolExecutor(max_workers=5) as ex:
        future_map = {ex.submit(fetch_prices_for_date, DEPARTURE, r): r for r in fetch_ret}
        for f in as_completed(future_map):
            r = future_map[f]
            try:
                results = f.result()
                for airline, info in results.items():
                    c = info["cheapest"]
                    ret_by_airline.setdefault(airline, []).append(
                        (r, c["total_fare"], c["partner"], c["total_duration_mins"]))
            except Exception:
                pass

    for airline, info in base_results.items():
        c = info["cheapest"]
        dep_by_airline.setdefault(airline, []).append(
            (DEPARTURE, c["total_fare"], c["partner"], c["total_duration_mins"]))
        ret_by_airline.setdefault(airline, []).append(
            (RETURN, c["total_fare"], c["partner"], c["total_duration_mins"]))

    dep_best = {}
    for airline, entries in dep_by_airline.items():
        best = min(entries, key=lambda x: x[1])
        dep_best[airline] = {"date": best[0], "price": best[1], "partner": best[2], "duration": best[3]}

    ret_best = {}
    for airline, entries in ret_by_airline.items():
        best = min(entries, key=lambda x: x[1])
        ret_best[airline] = {"date": best[0], "price": best[1], "partner": best[2], "duration": best[3]}

    return base_results, dep_best, ret_best


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_state(prices):
    serializable = {}
    for airline, info in prices.items():
        serializable[airline] = {
            "cheapest": info["cheapest"],
            "fastest": info["fastest"],
        }
    with open(STATE_FILE, "w") as f:
        json.dump(serializable, f, indent=2)


def describe_entry(entry, prefix="   "):
    total_dur_str = fmt_duration(entry["total_duration_mins"])
    out_stop_str = f"{entry['outbound_stops']} stop{'s' if entry['outbound_stops'] != 1 else ''}"
    in_stop_str = f"{entry['inbound_stops']} stop{'s' if entry['inbound_stops'] != 1 else ''}"
    return [
        f"{prefix}₹{entry['total_fare']:,.0f} via {entry['partner']}",
        f"{prefix}  ⏱ {total_dur_str} total  |  {out_stop_str} + {in_stop_str}",
        f"{prefix}  Outbound: {entry['outbound']}",
        f"{prefix}  Inbound:  {entry['inbound']}",
    ]


def format_alert(airline, curr_cheapest, curr_fastest, prev_cheapest, prev_fastest):
    msg = []
    msg.append(f"✈️ {airline} — BOM→YYZ")

    prev_price = prev_cheapest["total_fare"] if prev_cheapest else 0
    price_drop = prev_price - curr_cheapest["total_fare"]
    pct = round((price_drop / prev_price) * 100, 1) if prev_price else 0

    if price_drop > 0:
        msg.append(f"  🔻 Cheapest DROPPED: ₹{prev_price:,.0f} → ₹{curr_cheapest['total_fare']:,.0f} (-₹{price_drop:,.0f}, {pct}% off)")
    elif price_drop < 0:
        msg.append(f"  🔺 Cheapest ROSE: ₹{prev_price:,.0f} → ₹{curr_cheapest['total_fare']:,.0f} (+₹{abs(price_drop):,.0f})")
    else:
        msg.append(f"  🪙 Cheapest: ₹{curr_cheapest['total_fare']:,.0f} (unchanged)")
    msg.extend(describe_entry(curr_cheapest, "    "))

    prev_fast_dur = prev_fastest.get("total_duration_mins", 0) if prev_fastest else 0
    curr_fast_dur = curr_fastest["total_duration_mins"]

    is_same_as_cheapest = (
        curr_fastest["total_fare"] == curr_cheapest["total_fare"]
        and curr_fastest["partner"] == curr_cheapest["partner"]
    )

    if not is_same_as_cheapest:
        dur_diff = prev_fast_dur - curr_fast_dur if prev_fast_dur else 0
        if dur_diff > 0:
            msg.append(f"  ⚡ Fastest GOT FASTER: {fmt_duration(prev_fast_dur)} → {fmt_duration(curr_fast_dur)} ({dur_diff}m shorter)")
        elif dur_diff < 0:
            msg.append(f"  ⏳ Fastest GOT SLOWER: {fmt_duration(prev_fast_dur)} → {fmt_duration(curr_fast_dur)} ({abs(dur_diff)}m longer)")
        else:
            msg.append(f"  ⚡ Fastest: {fmt_duration(curr_fast_dur)} at ₹{curr_fastest['total_fare']:,.0f}")
        msg.extend(describe_entry(curr_fastest, "    "))

    msg.append(f"  🔗 https://offers.reward360.in/v1/flight/int?flightfrom=BOM&flightto=YYZ&departure={DEPARTURE}&arrival={RETURN}")
    return "\n".join(msg)


def format_initial(airline, cheapest, fastest):
    msg = []
    msg.append(f"✈️ {airline} — BOM→YYZ")
    msg.append(f"  🪙 Cheapest:")
    msg.extend(describe_entry(cheapest, "    "))

    is_same = (
        cheapest["total_fare"] == fastest["total_fare"]
        and cheapest["partner"] == fastest["partner"]
    )
    if not is_same:
        msg.append(f"  ⚡ Fastest ({fmt_duration(fastest['total_duration_mins'])}):")
        msg.extend(describe_entry(fastest, "    "))

    msg.append(f"  🔗 https://offers.reward360.in/v1/flight/int?flightfrom=BOM&flightto=YYZ&departure={DEPARTURE}&arrival={RETURN}")
    return "\n".join(msg)


def format_flex_line(airline, preferred_price, best):
    saving = preferred_price - best["price"]
    if saving > 0:
        dir_str = f"(-₹{saving:,.0f})"
    elif saving < 0:
        dir_str = f"(+₹{abs(saving):,.0f})"
    else:
        dir_str = ""
    return f"  {airline}: ₹{best['price']:,.0f} on {best['date']} via {best['partner']}  {dir_str}"


def send_telegram(message):
    """Send a message via Telegram bot API."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("  [Telegram: skipped — no TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID set]")
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = json.dumps({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
        })
        result = subprocess.run(
            ["curl", "-s", "--max-time", "10", url,
             "-H", "Content-Type: application/json",
             "-d", payload],
            capture_output=True, text=True, timeout=15)
        resp = json.loads(result.stdout)
        if resp.get("ok"):
            print("  [Telegram: ✓ sent]")
            return True
        else:
            print(f"  [Telegram: ✗ {resp.get('description', 'unknown error')}]")
            return False
    except Exception as e:
        print(f"  [Telegram: ✗ {e}]")
        return False


def main():
    try:
        base_results = fetch_prices()
    except Exception as e:
        print(f"❌ Error fetching prices: {e}")
        sys.exit(1)

    previous = load_state()
    save_state(base_results)

    dep_flex = {}
    ret_flex = {}
    try:
        _, dep_flex, ret_flex = fetch_flex_prices()
    except Exception:
        pass

    changes = []
    first_time = not previous

    for airline in sorted(TARGET_AIRLINES):
        if airline not in base_results:
            changes.append(f"❌ {airline}: No flights found on this route.")
            continue

        curr_cheapest = base_results[airline]["cheapest"]
        curr_fastest = base_results[airline]["fastest"]
        in_range = MIN_PRICE <= curr_cheapest["total_fare"] <= MAX_PRICE

        if first_time or airline not in previous:
            if in_range:
                changes.append(format_initial(airline, curr_cheapest, curr_fastest))
                changes.append("")
            else:
                changes.append(
                    f"📍 {airline}: ₹{curr_cheapest['total_fare']:,.0f} (outside ₹{MIN_PRICE:,}–₹{MAX_PRICE:,} target range)"
                )
        else:
            prev_cheapest = previous[airline].get("cheapest", {})
            prev_fastest = previous[airline].get("fastest", {})

            price_changed = (
                curr_cheapest["total_fare"] != prev_cheapest.get("total_fare")
                or curr_cheapest["partner"] != prev_cheapest.get("partner")
            )
            duration_changed = (
                curr_fastest["total_duration_mins"] != prev_fastest.get("total_duration_mins")
                or curr_fastest["total_fare"] != prev_fastest.get("total_fare")
                or curr_fastest["partner"] != prev_fastest.get("partner")
                or curr_cheapest["total_duration_mins"] != prev_cheapest.get("total_duration_mins")
            )

            if price_changed or duration_changed:
                if in_range:
                    changes.append(format_alert(airline, curr_cheapest, curr_fastest, prev_cheapest, prev_fastest))
                    changes.append("")
                else:
                    changes.append(
                        f"📍 {airline}: ₹{curr_cheapest['total_fare']:,.0f} (→ outside target range)"
                    )
            else:
                changes.append(
                    f"✓ {airline}: ₹{curr_cheapest['total_fare']:,.0f} / {fmt_duration(curr_fastest['total_duration_mins'])} (unchanged)"
                )

    # Build full output
    dep_range = date_list(DEPARTURE, DATE_FLEX)
    ret_range = date_list(RETURN, DATE_FLEX)

    header = f"🛫 BOM→YYZ Flight Monitor ({datetime.now().strftime('%d %b %H:%M')})"
    output_lines = [header]
    output_lines.append(f"   👥 {PASSENGERS_LABEL} | {DEPARTURE} → {RETURN} | {TRAVEL_CLASS}")
    output_lines.append(f"   💰 Target range: ₹{MIN_PRICE:,} – ₹{MAX_PRICE:,}  |  Alerts only in this band")
    output_lines.append(f"   📅 Flex: ±{DATE_FLEX}d — dep {dep_range[0]}–{dep_range[-1]}, ret {ret_range[0]}–{ret_range[-1]}")
    output_lines.append("=" * 48)

    if dep_flex:
        output_lines.append("\n📆 Best departure dates:")
        for airline in sorted(TARGET_AIRLINES):
            if airline in dep_flex:
                pref_price = base_results.get(airline, {}).get("cheapest", {}).get("total_fare", 0)
                output_lines.append(format_flex_line(airline, pref_price, dep_flex[airline]))
            else:
                output_lines.append(f"  {airline}: no data")

    if ret_flex:
        output_lines.append("\n📆 Best return dates:")
        for airline in sorted(TARGET_AIRLINES):
            if airline in ret_flex:
                pref_price = base_results.get(airline, {}).get("cheapest", {}).get("total_fare", 0)
                output_lines.append(format_flex_line(airline, pref_price, ret_flex[airline]))
            else:
                output_lines.append(f"  {airline}: no data")

    output_lines.append("")
    output_lines.append("\n".join(changes))

    full_output = "\n".join(output_lines)
    print(full_output)

    # Send Telegram notification if prices changed significantly
    has_real_change = any(
        line.startswith(("✈️", "🪙", "🔻", "🔺", "⚡", "⏳"))
        for line in changes
    )
    if has_real_change:
        print("\n--- Change detected, sending Telegram notification ---")
        send_telegram(full_output)
    else:
        print("\n--- No price changes detected ---")


if __name__ == "__main__":
    main()
