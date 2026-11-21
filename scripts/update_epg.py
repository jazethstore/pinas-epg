#!/usr/bin/env python3
"""Refresh matching channels in the family XMLTV file from the EPG.PW public feed.

Only channels that can be positively matched by display name are replaced.
Unmatched channels retain their existing entries; the job reports match coverage.
"""
from datetime import datetime, timedelta, timezone
from html import unescape
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
    "tfc": ["TFC", "The Filipino Channel"],
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


def clickthecity_schedule(url):
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; PinasEPG/1.0)"})
    with urlopen(req, timeout=45) as response:
        raw = response.read().decode("utf-8", errors="replace")

    page_text = unescape(re.sub(r"<[^>]+>", " ", raw))
    page_text = re.sub(r"\s+", " ", page_text)
    date_match = re.search(
        r"(?:programming guide for|schedule for) ([A-Za-z]+ \d{1,2}, \d{4})",
        page_text, re.I
    )
    if not date_match:
        raise RuntimeError("ClickTheCity schedule date was not found")
    schedule_date = datetime.strptime(date_match.group(1), "%B %d, %Y").date()

    entries = []
    # Prefer individual list items so each programme title remains intact.
    for item in re.findall(r"<li\b[^>]*>(.*?)</li>", raw, re.I | re.S):
        text = unescape(re.sub(r"<[^>]+>", " ", item))
        text = re.sub(r"\s+", " ", text).strip()
        match = re.match(r"(\d{1,2}:\d{2}\s*[AP]M)\s*[–—-]\s*(.+)$", text, re.I)
        if match:
            title = re.sub(r"\s+", " ", match.group(2)).strip(" .")
            if title and not title.lower().startswith("view full"):
                entries.append((match.group(1), title))

    # Fallback for markup where schedule rows are not list elements.
    if len(entries) < 5:
        entries = []
        pattern = re.compile(
            r"(\d{1,2}:\d{2}\s*[AP]M)\s*[–—-]\s*(.*?)"
            r"(?=\s+\d{1,2}:\d{2}\s*[AP]M\s*[–—-]|$)", re.I
        )
        for match in pattern.finditer(page_text):
            title = re.sub(r"\s+", " ", match.group(2)).strip(" .")
            title = re.split(r"View Full Philippine TV Schedule", title, flags=re.I)[0].strip()
            if title:
                entries.append((match.group(1), title))

    if len(entries) < 8:
        raise RuntimeError(f"ClickTheCity returned only {len(entries)} programme rows")

    la_entries = []
    seen = set()
    for time_text, title in entries:
        local_time = datetime.strptime(time_text.upper().replace(" ", ""), "%I:%M%p").time()
        manila = datetime.combine(schedule_date, local_time).replace(
            tzinfo=ZoneInfo("Asia/Manila")
        )
        start = manila.astimezone(LA)
        key = (start, title)
        if key not in seen:
            la_entries.append((start, title))
            seen.add(key)
    la_entries.sort(key=lambda item: item[0])
    return la_entries


def replace_clickthecity_channel(root, channel_id, url):
    entries = clickthecity_schedule(url)
    if len(entries) < 8:
        raise RuntimeError(f"Not enough schedule data for {channel_id}")

    programmes = root.findall(f"programme[@channel='{channel_id}']")
    first_start = entries[0][0]
    last_start = entries[-1][0]
    last_stop = last_start + timedelta(hours=2)

    # Remove only rows overlapping the schedule window being refreshed.
    for old in programmes:
        old_start = parse_dt(old.get("start"))
        old_stop = parse_dt(old.get("stop"))
        if old_start and old_stop and old_start < last_stop and old_stop > first_start:
            root.remove(old)

    for index, (start, title) in enumerate(entries):
        stop = entries[index + 1][0] if index + 1 < len(entries) else last_stop
        if stop <= start:
            continue
        program = ET.Element("programme", {
            "channel": channel_id,
            "start": fmt_dt(start),
            "stop": fmt_dt(stop),
        })
        ET.SubElement(program, "title", {"lang": "en"}).text = title
        ET.SubElement(program, "desc").text = (
            f"ClickTheCity schedule; Philippine time converted to America/Los_Angeles."
        )
        root.append(program)
    return len(entries)


def fill_tfc_unlisted_gaps(root):
    # Preserve known programme titles. For long gaps in the third-party feed,
    # use a clearly labelled generic block rather than leaving the guide blank.
    items = []
    for program in root.findall("programme[@channel='tfc']"):
        start, stop = parse_dt(program.get("start")), parse_dt(program.get("stop"))
        if start and stop and stop > start:
            items.append((start, stop))
    items.sort(key=lambda item: item[0])
    added = 0
    for (_, previous_stop), (next_start, _) in zip(items, items[1:]):
        gap = next_start - previous_stop
        if gap >= timedelta(minutes=90):
            filler = ET.Element("programme", {
                "channel": "tfc",
                "start": fmt_dt(previous_stop),
                "stop": fmt_dt(next_start),
            })
            ET.SubElement(filler, "title", {"lang": "en"}).text = "TFC Programming"
            ET.SubElement(filler, "desc").text = (
                "No individual programme title was supplied for this time slot; "
                "check the official MyTFC Pacific guide."
            )
            root.append(filler)
            added += 1
    return added

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

    # TFC is matched from the EPG.PW channel feed; its official MyTFC guide is
    # the reference for Pacific time. Do not replace the ClickTheCity channels
    # with EPG.PW because those feeds produced duplicate/incomplete movie rows.
    tfc_count = len(current.findall("programme[@channel='tfc']"))
    if tfc_count < 10:
        raise RuntimeError("TFC feed returned too few entries; existing schedule retained.")

    # Refresh PBO and Cinema One from ClickTheCity (Philippine schedule times
    # are converted to America/Los_Angeles by the helper).
    for channel_id, url in (
        ("pbo", "https://www.clickthecity.com/tv/channels/pbo"),
        ("cinemaone", "https://www.clickthecity.com/tv/channels/cinema-one"),
    ):
        try:
            count = replace_clickthecity_channel(current, channel_id, url)
            print(f"{channel_id}: refreshed {count} ClickTheCity programmes")
        except Exception as exc:
            print(f"{channel_id}: kept previous schedule; ClickTheCity refresh failed: {exc}")

    filler_count = fill_tfc_unlisted_gaps(current)
    print(f"TFC: {tfc_count} programmes; added {filler_count} labelled unlisted-time blocks")

    # Keep XMLTV programme rows chronological for IPTV clients.
    programme_rows = list(current.findall("programme"))
    for program in programme_rows:
        current.remove(program)
    programme_rows.sort(key=lambda p: p.get("start", ""))
    current.extend(programme_rows)

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
