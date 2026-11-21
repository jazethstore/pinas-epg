#!/usr/bin/env python3
"""Refresh matching channels in the family XMLTV file from the EPG.PW public feed.

Only channels that can be positively matched by display name are replaced.
Unmatched channels retain their existing entries; the job reports match coverage.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.request import Request, urlopen
import base64
import os
import re
import xml.etree.ElementTree as ET

TARGET = "pinas_epg001.xml"
LA = ZoneInfo("America/Los_Angeles")
ALIASES = {
    "gmapinoytv_asia": ["GMA Pinoy TV", "GMA Pinoy"],
    "anc_singapore": ["ANC", "ABS-CBN News Channel"],
    "gma_news_tv_410428": ["GMA News TV", "GTV"],
    "gma_life_tv_410427": ["GMA Life TV", "GMA Life"],
    "viva_cinema": ["VIVA Cinema", "Viva Cinemas"],
    "solar_flix": ["Solar Flix", "SolarFlix"],
    "gma7": ["GMA 7", "GMA", "GMA-7"],
    "kapamilya_channel": ["Kapamilya Channel"],
    "one_ph": ["One PH", "OnePH"],
}

def norm(value):
    return re.sub(r"[^a-z0-9]+", "", value.lower())

def parse_dt(value):
    # XMLTV date: YYYYMMDDHHMMSS +HHMM (or -HHMM).
    m = re.fullmatch(r"(\d{14})\s*([+-]\d{4})?", value or "")
    if not m:
        return None
    dt = datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
    zone = m.group(2)
    if zone:
        sign = 1 if zone[0] == "+" else -1
        offset = timezone(sign * timedelta(hours=int(zone[1:3]), minutes=int(zone[3:5])))
        dt = dt.replace(tzinfo=offset)
    else:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LA)

def fmt_dt(dt):
    return dt.strftime("%Y%m%d%H%M%S %z")

def main():
    with open(TARGET, "rb") as f:
        current = ET.fromstring(f.read())

    tz_b64 = base64.b64encode(b"America/Los_Angeles").decode("ascii")
    feed_url = "https://epg.pw/api/epg.xml?lang=en&timezone=" + tz_b64
    req = Request(feed_url, headers={"User-Agent": "pinas-epg-updater/1.0"})
    with urlopen(req, timeout=90) as response:
        feed_bytes = response.read()
    feed = ET.fromstring(feed_bytes)

    source_names = {}
    for channel in feed.findall("channel"):
        names = [x.text.strip() for x in channel.findall("display-name") if x.text and x.text.strip()]
        source_names[channel.get("id")] = names

    source_by_name = {}
    for source_id, names in source_names.items():
        for name in names:
            source_by_name.setdefault(norm(name), []).append(source_id)

    source_to_target = {}
    matched = {}
    for target_id, aliases in ALIASES.items():
        candidates = []
        for alias in aliases:
            candidates.extend(source_by_name.get(norm(alias), []))
        candidates = list(dict.fromkeys(candidates))
        if len(candidates) == 1:
            source_to_target[candidates[0]] = target_id
            matched[target_id] = candidates[0]

    now = datetime.now(timezone.utc).astimezone(LA)
    horizon = now + timedelta(days=14)
    replacement = {target_id: [] for target_id in matched}
    for program in feed.findall("programme"):
        target_id = source_to_target.get(program.get("channel"))
        if not target_id:
            continue
        start = parse_dt(program.get("start"))
        stop = parse_dt(program.get("stop"))
        if not start or not stop or stop <= now or start >= horizon or stop <= start:
            continue
        program.set("channel", target_id)
        program.set("start", fmt_dt(start))
        program.set("stop", fmt_dt(stop))
        replacement[target_id].append(program)

    # Normalize malformed XMLTV timestamps before validating or publishing.
    # Some manually imported schedule rows can contain two extra trailing zeroes.
    for program in current.findall("programme"):
        for attr in ("start", "stop"):
            value = program.get(attr, "")
            if re.fullmatch(r"\d{16}\s*[+-]\d{4}", value):
                program.set(attr, value[:14] + " " + value[-5:])

    # Require a substantial live feed before changing anything.
    total_new = sum(len(items) for items in replacement.values())
    if len(matched) < 5 or total_new < 100:
        raise RuntimeError(
            f"Feed validation failed: matched {len(matched)}/9 EPG.PW-managed channels and "
            f"{total_new} future programmes. Existing XML left untouched."
        )

    # Keep channel metadata/logos from the existing file. Replace only positively
    # matched channels; leave unmatched channel schedules intact.
    for target_id, items in replacement.items():
        for old in list(current.findall(f"programme[@channel='{target_id}']")):
            current.remove(old)
        current.extend(items)

    current.set("generator-info-name", "Pinas EPG - daily refreshed; America/Los_Angeles")
    ET.indent(current, space="  ")
    output = ET.tostring(current, encoding="utf-8", xml_declaration=True)
    with open(TARGET, "wb") as f:
        f.write(output)

    print(f"Matched {len(matched)}/9 EPG.PW-managed channels; loaded {total_new} future programmes.")
    for target_id in ALIASES:
        print(f"{target_id}: {'updated' if target_id in matched else 'kept existing schedule'}")

if __name__ == "__main__":
    main()
