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
    "cinemaone": ["Cinema One", "Cinema 1"],
    "pbo": ["PBO", "Pinoy Box Office"],
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
    date_match = re.search(r"([A-Za-z]+ \d{1,2}, \d{4})", page_text, re.I)
    if date_match:
        schedule_date = datetime.strptime(date_match.group(1), "%B %d, %Y").date()
    else:
        # ClickTheCity sometimes omits date metadata from its server-rendered HTML.
        # Its channel pages show today's schedule, so use the Philippines date.
        schedule_date = datetime.now(ZoneInfo("Asia/Manila")).date()

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
            title = re.sub(r"\s+\d+\.$", "", title)
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



def refresh_epgpw_date_channel(root, target_channel_id, source_channel_id):
    """Refresh one EPG.PW channel for today and tomorrow in Philippines date."""
    manila_today = datetime.now(ZoneInfo("Asia/Manila")).date()
    rows = []
    for day_offset in (0, 1):
        schedule_date = manila_today + timedelta(days=day_offset)
        url = (
            "https://www.epg.pw/api/epg.xml"
            f"?lang=en&date={schedule_date:%Y%m%d}&channel_id={source_channel_id}"
        )
        req = Request(url, headers={"User-Agent": "pinas-epg-updater/1.0"})
        with urlopen(req, timeout=60) as response:
            day_feed = ET.fromstring(response.read())
        for program in day_feed.findall("programme"):
            start = parse_dt(program.get("start"))
            stop = parse_dt(program.get("stop"))
            if not start or not stop or stop <= start:
                continue
            program.set("channel", target_channel_id)
            program.set("start", fmt_dt(start))
            program.set("stop", fmt_dt(stop))
            rows.append(program)

    # Deduplicate by exact interval and title; retain the source programme text.
    unique = {}
    for program in rows:
        title = " ".join((program.findtext("title") or "").split())
        unique[(program.get("start"), program.get("stop"), title)] = program
    rows = sorted(unique.values(), key=lambda p: p.get("start", ""))
    if len(rows) < 8:
        raise RuntimeError(
            f"EPG.PW channel {source_channel_id} returned only {len(rows)} programmes"
        )

    first_start = parse_dt(rows[0].get("start"))
    last_stop = max(parse_dt(p.get("stop")) for p in rows)
    for old in list(root.findall(f"programme[@channel='{target_channel_id}']")):
        old_start = parse_dt(old.get("start"))
        old_stop = parse_dt(old.get("stop"))
        if old_start and old_stop and old_start < last_stop and old_stop > first_start:
            root.remove(old)
    root.extend(rows)
    return len(rows)


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

    # Normalize malformed XMLTV timestamps left by earlier manual imports.
    for program in current.findall("programme"):
        for attr in ("start", "stop"):
            value = program.get(attr, "")
            if re.fullmatch(r"\\d{16}\\s*[+-]\\d{4}", value):
                program.set(attr, value[:14] + " " + value[-5:])

    tz_b64 = base64.b64encode(b"America/Los_Angeles").decode("ascii")
    now = datetime.now(timezone.utc).astimezone(LA)
    horizon = now + timedelta(days=14)

    # The old all-channels EPG.PW endpoint now returns 404. Try it as an
    # optional source, but do not let that take down the daily refresh.
    matched = {}
    applied_targets = set()
    total_new = 0
    feed = None
    feed_url = "https://epg.pw/xmltv/epg_PH.xml"
    try:
        req = Request(feed_url, headers={"User-Agent": "pinas-epg-updater/1.0"})
        with urlopen(req, timeout=90) as response:
            feed = ET.fromstring(response.read())
    except Exception as exc:
        print(f"All-channel EPG.PW feed unavailable; keeping existing general-channel data: {exc}")

    if feed is not None:
        source_names = {}
        for channel in feed.findall("channel"):
            names = [x.text.strip() for x in channel.findall("display-name") if x.text and x.text.strip()]
            source_names[channel.get("id")] = names

        source_by_name = {}
        for source_id, names in source_names.items():
            for name in names:
                source_by_name.setdefault(norm(name), []).append(source_id)

        source_to_target = {}
        for target_id, aliases in ALIASES.items():
            candidates = []
            for alias in aliases:
                candidates.extend(source_by_name.get(norm(alias), []))
            candidates = list(dict.fromkeys(candidates))
            if len(candidates) == 1:
                source_to_target[candidates[0]] = target_id
                matched[target_id] = candidates[0]

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

        total_new = sum(len(items) for items in replacement.values())
        if len(matched) >= 5 and total_new >= 50:
            for target_id, items in replacement.items():
                if target_id in {"pbo", "cinemaone"} or len(items) < 4:
                    continue  # ClickTheCity is preferred; do not erase sparse channels.
                for old in list(current.findall(f"programme[@channel='{target_id}']")):
                    current.remove(old)
                current.extend(items)
                applied_targets.add(target_id)
        else:
            print(f"General feed validation failed ({len(matched)} matches, {total_new} programmes); keeping existing schedules.")

    # TFC has a stable channel-specific EPG.PW endpoint. Its times are converted
    # from the feed's timezone to Los Angeles. MyTFC's Pacific guide remains the
    # preferred reference when checking the programme titles.
    try:
        tfc_url = "https://epg.pw/api/epg.xml?channel_id=474984"
        req = Request(tfc_url, headers={"User-Agent": "pinas-epg-updater/1.0"})
        with urlopen(req, timeout=60) as response:
            tfc_feed = ET.fromstring(response.read())
        tfc_rows = []
        for program in tfc_feed.findall("programme"):
            start = parse_dt(program.get("start"))
            stop = parse_dt(program.get("stop"))
            if not start or not stop or stop <= now or start >= horizon or stop <= start:
                continue
            program.set("channel", "tfc")
            program.set("start", fmt_dt(start))
            program.set("stop", fmt_dt(stop))
            tfc_rows.append(program)
        if len(tfc_rows) >= 10:
            for old in list(current.findall("programme[@channel='tfc']")):
                current.remove(old)
            current.extend(tfc_rows)
            applied_targets.add("tfc")
            print(f"TFC: refreshed {len(tfc_rows)} entries from channel 474984.")
        else:
            print(f"TFC channel feed returned only {len(tfc_rows)} entries; retained existing TFC schedule.")
    except Exception as exc:
        print(f"TFC channel feed unavailable; retained existing TFC schedule: {exc}")

    # Refresh PBO and Cinema One from ClickTheCity (Philippine schedule times
    # are converted to America/Los_Angeles by the helper).
    for channel_id, url in (
        ("pbo", "https://www.clickthecity.com/tv/channels/pbo"),
        ("cinemaone", "https://www.clickthecity.com/tv/channels/cinema-one"),
    ):
        try:
            count = replace_clickthecity_channel(current, channel_id, url)
            applied_targets.add(channel_id)
            print(f"{channel_id}: refreshed {count} ClickTheCity programmes")
        except Exception as exc:
            fallback = replacement.get(channel_id, []) if feed is not None else []

            # Use EPG.PW's date-specific channel API for Cinema One Global.
            # The date is calculated from the Philippines calendar and the
            # returned programme times are converted to Los Angeles.
            if channel_id == "cinemaone":
                try:
                    count = refresh_epgpw_date_channel(current, "cinemaone", "492842")
                    applied_targets.add("cinemaone")
                    print(f"Cinema One: refreshed {count} entries from EPG.PW channel 492842.")
                    continue
                except Exception as cinema_exc:
                    print(f"Cinema One EPG.PW feed unavailable; retained previous schedule: {cinema_exc}")

            # PBO also has a dedicated EPG.PW endpoint. Use it only when it
            # supplies a substantial schedule; never replace a good schedule
            # with a handful of incomplete rows.
            if channel_id == "pbo":
                try:
                    req = Request(
                        "https://epg.pw/api/epg.xml?channel_id=575516",
                        headers={"User-Agent": "pinas-epg-updater/1.0"},
                    )
                    with urlopen(req, timeout=60) as response:
                        pbo_feed = ET.fromstring(response.read())
                    pbo_rows = []
                    for program in pbo_feed.findall("programme"):
                        start = parse_dt(program.get("start"))
                        stop = parse_dt(program.get("stop"))
                        if not start or not stop or stop <= now or start >= horizon or stop <= start:
                            continue
                        program.set("channel", "pbo")
                        program.set("start", fmt_dt(start))
                        program.set("stop", fmt_dt(stop))
                        pbo_rows.append(program)
                    if len(pbo_rows) >= 8:
                        fallback = pbo_rows
                        print(f"PBO: dedicated EPG.PW feed supplied {len(pbo_rows)} programmes.")
                    else:
                        print(f"PBO: dedicated EPG.PW feed supplied only {len(pbo_rows)} programmes; not replacing the current schedule.")
                except Exception as pbo_exc:
                    print(f"PBO: dedicated EPG.PW feed unavailable: {pbo_exc}")

            if len(fallback) >= 8:
                for old in list(current.findall(f"programme[@channel='{channel_id}']")):
                    current.remove(old)
                current.extend(fallback)
                applied_targets.add(channel_id)
                print(f"{channel_id}: ClickTheCity unavailable; used {len(fallback)} EPG.PW fallback programmes.")
            else:
                print(f"{channel_id}: kept previous schedule; ClickTheCity refresh failed: {exc}")

    filler_count = fill_tfc_unlisted_gaps(current)
    print(f"TFC: added {filler_count} clearly labelled blocks where the EPG source has no title.")

    # Keep XMLTV programme rows chronological for IPTV clients.
    programme_rows = list(current.findall("programme"))
    for program in programme_rows:
        current.remove(program)
    programme_rows.sort(key=lambda program: program.get("start", ""))
    current.extend(programme_rows)

    current.set("generator-info-name", "Pinas EPG - refreshed every 3 hours; America/Los_Angeles")
    ET.indent(current, space="  ")
    output = ET.tostring(current, encoding="utf-8", xml_declaration=True)
    with open(TARGET, "wb") as f:
        f.write(output)

    print(f"General channels matched: {len(matched)}; future programmes loaded: {total_new}.")
    for target_id in ALIASES:
        print(f"{target_id}: {'updated' if target_id in applied_targets else 'kept existing schedule'}")

if __name__ == "__main__":
    main()
