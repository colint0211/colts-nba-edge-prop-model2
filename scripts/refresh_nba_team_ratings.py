#!/usr/bin/env python3
"""Refresh 30-team ratings from Basketball-Reference's public team tables.

NBA Stats is timing out from both Vercel and GitHub-hosted runners. This
collector uses Basketball-Reference's season ratings and advanced team tables.
It fails closed: no partial, stale, or invented rows are published.
"""
import json
import re
import time
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

import pandas as pd
import requests

TEAM_CODES = {
    "Atlanta Hawks":"ATL","Boston Celtics":"BOS","Brooklyn Nets":"BKN",
    "Charlotte Hornets":"CHA","Chicago Bulls":"CHI","Cleveland Cavaliers":"CLE",
    "Dallas Mavericks":"DAL","Denver Nuggets":"DEN","Detroit Pistons":"DET",
    "Golden State Warriors":"GSW","Houston Rockets":"HOU","Indiana Pacers":"IND",
    "Los Angeles Clippers":"LAC","Los Angeles Lakers":"LAL","Memphis Grizzlies":"MEM",
    "Miami Heat":"MIA","Milwaukee Bucks":"MIL","Minnesota Timberwolves":"MIN",
    "New Orleans Pelicans":"NOP","New York Knicks":"NYK","Oklahoma City Thunder":"OKC",
    "Orlando Magic":"ORL","Philadelphia 76ers":"PHI","Phoenix Suns":"PHX",
    "Portland Trail Blazers":"POR","Sacramento Kings":"SAC","San Antonio Spurs":"SAS",
    "Toronto Raptors":"TOR","Utah Jazz":"UTA","Washington Wizards":"WAS",
}
BASE = "https://www.basketball-reference.com/leagues"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.basketball-reference.com/",
}

def season_code(now):
    start = now.year if now.month >= 7 else now.year - 1
    return start + 1, f"{start}-{str(start + 1)[-2:]}"

def fetch_tables(url):
    last = None
    for attempt in range(3):
        try:
            response = requests.get(url, headers=HEADERS, timeout=25)
            response.raise_for_status()
            html = response.text
            if "Team Ratings Table" not in html and "Advanced Stats Table" not in html:
                # HTML comments occasionally contain the table markup on B-Ref.
                pass
            # Basketball-Reference frequently wraps table markup in HTML
            # comments. pandas.read_html on the raw page silently misses it.
            fragments = [html]
            fragments.extend(re.findall(r"<!--(.*?)-->", html, flags=re.DOTALL))
            tables = []
            for fragment in fragments:
                try:
                    tables.extend(pd.read_html(StringIO(fragment)))
                except (ValueError, ImportError):
                    continue
            if tables:
                return tables
            title = re.search(r"<title>(.*?)</title>", html, flags=re.I | re.S)
            page_title = re.sub(r"\\s+", " ", title.group(1)).strip() if title else "unknown"
            raise RuntimeError(f"No HTML tables found at {url}; page title={page_title}; response_bytes={len(html)}")
        except Exception as exc:
            last = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Basketball-Reference fetch failed for {url}: {last}")

def flat_columns(frame):
    cols=[]
    for col in frame.columns:
        if isinstance(col, tuple):
            vals=[str(v).strip() for v in col if str(v).strip() and not str(v).startswith("Unnamed")]
            cols.append(vals[-1] if vals else "")
        else:
            cols.append(str(col).strip())
    frame=frame.copy()
    frame.columns=cols
    return frame

def find_table(tables, required):
    for raw in tables:
        frame=flat_columns(raw)
        cols=set(frame.columns)
        if required.issubset(cols):
            return frame
    raise RuntimeError("Could not find table with columns: " + ", ".join(sorted(required)))

def num(value):
    if pd.isna(value): return None
    text=str(value).strip().replace(",","")
    if text in ("","—","-"): return None
    return float(text)

def main():
    now=datetime.now(timezone.utc)
    year, season=season_code(now)
    ratings_url=f"{BASE}/NBA_{year}_ratings.html"
    summary_url=f"{BASE}/NBA_{year}.html"
    ratings=find_table(fetch_tables(ratings_url), {"Team","ORtg","DRtg"})
    advanced=find_table(fetch_tables(summary_url), {"Team","Pace","TS%"})
    adv_by_name={}
    for _, item in advanced.iterrows():
        name=re.sub(r"[*]+$","",str(item["Team"])).strip()
        if name in TEAM_CODES:
            adv_by_name[TEAM_CODES[name]]=item
    teams={}
    for _, item in ratings.iterrows():
        name=re.sub(r"[*]+$","",str(item["Team"])).strip()
        code=TEAM_CODES.get(name)
        if not code or code not in adv_by_name:
            continue
        adv=adv_by_name[code]
        try:
            ortg=num(item["ORtg"]); drtg=num(item["DRtg"])
            pace=num(adv["Pace"]); ts=num(adv["TS%"])
            efg=num(adv["eFG%"]); tov=num(adv["TOV%"]); oreb=num(adv["ORB%"])
            wins=num(item.get("W")); losses=num(item.get("L"))
            games=int((wins or 0)+(losses or 0))
        except (TypeError,ValueError,KeyError):
            continue
        values=[ortg,drtg,pace,ts,efg,tov,oreb]
        if any(v is None for v in values) or not (70 < pace < 115 and 80 < ortg < 140 and 80 < drtg < 140):
            continue
        teams[code]={
            "team":code,"games":games,"ortg":ortg,"drtg":drtg,
            "net":ortg-drtg,"pace":pace,"efg":efg/100 if efg>1 else efg,
            "tov":tov/100 if tov>1 else tov,"oreb":oreb/100 if oreb>1 else oreb,
            "ts":ts/100 if ts>1 else ts,
        }
    missing=sorted(set(TEAM_CODES.values())-set(teams))
    if len(teams)!=30:
        raise RuntimeError(f"Refusing to publish incomplete Basketball-Reference data: {len(teams)}/30; missing={missing}")
    payload={
        "source":"Basketball-Reference NBA team ratings and advanced stats",
        "season":season,"fetched_at":now.isoformat(),"team_count":30,"teams":teams,
        "source_urls":[ratings_url,summary_url],
    }
    output=Path("data/nba_team_advanced.json")
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(payload,separators=(",",":"),allow_nan=False)+"\n",encoding="utf-8")
    print(f"Validated 30 Basketball-Reference team rows for {season}; ORtg/DRtg, pace and four factors saved to {output}")

if __name__=="__main__":
    main()
