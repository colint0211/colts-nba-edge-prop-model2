#!/usr/bin/env python3
"""Fetch and validate the official NBA Stats Advanced team table."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from nba_api.stats.endpoints import leaguedashteamstats
from nba_api.stats.library.http import NBAStatsHTTP
from curl_cffi import requests as curl_requests

TEAM_CODES = {
    "ATL","BOS","BKN","CHA","CHI","CLE","DAL","DEN","DET","GSW",
    "HOU","IND","LAC","LAL","MEM","MIA","MIL","MIN","NOP","NYK",
    "OKC","ORL","PHI","PHX","POR","SAC","SAS","TOR","UTA","WAS",
}

def main():
    now = datetime.now(timezone.utc)
    season_start = now.year if now.month >= 7 else now.year - 1
    season = f"{season_start}-{str(season_start + 1)[-2:]}"
    headers = {
        "Host": "stats.nba.com",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nba.com/",
        "Origin": "https://www.nba.com",
        "Connection": "keep-alive",
        "x-nba-stats-origin": "stats",
        "x-nba-stats-token": "true",
    }
    session = curl_requests.Session(impersonate="chrome120", http_version="v1")
    NBAStatsHTTP.get_session = lambda self: session
    endpoint = leaguedashteamstats.LeagueDashTeamStats(
        season=season,
        season_type_all_star="Regular Season",
        measure_type_detailed_defense="Advanced",
        per_mode_detailed="PerGame",
        league_id_nullable="00",
        last_n_games=0,
        month=0,
        opponent_team_id=0,
        pace_adjust="N",
        plus_minus="N",
        rank="N",
        timeout=60,
        headers=headers,
    )
    frames = endpoint.get_data_frames()
    if not frames:
        raise RuntimeError("NBA Stats returned no data frame.")
    teams = {}
    for item in frames[0].to_dict(orient="records"):
        code = str(item.get("TEAM_ABBREVIATION") or "").upper()
        if code not in TEAM_CODES:
            continue
        try:
            row = {
                "team": code,
                "games": int(item.get("GP") or 0),
                "ortg": float(item["OFF_RATING"]),
                "drtg": float(item["DEF_RATING"]),
                "net": float(item["NET_RATING"]),
                "pace": float(item["PACE"]),
                "efg": float(item.get("EFG_PCT") or 0),
                "tov": float(item.get("TM_TOV_PCT") or 0),
                "oreb": float(item.get("OREB_PCT") or 0),
                "ts": float(item.get("TS_PCT") or 0),
            }
        except (KeyError, TypeError, ValueError):
            continue
        teams[code] = row
    missing = sorted(TEAM_CODES - set(teams))
    if len(teams) != 30 or missing:
        raise RuntimeError(f"Refusing to publish incomplete NBA Stats table: {len(teams)}/30; missing={missing}")
    payload = {
        "source": "NBA Stats leaguedashteamstats Advanced",
        "season": season,
        "fetched_at": now.isoformat(),
        "team_count": len(teams),
        "teams": teams,
    }
    output = Path("data/nba_team_advanced.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, separators=(",", ":"), allow_nan=False) + "\n", encoding="utf-8")
    print(f"Validated and wrote {len(teams)} official NBA Stats teams for {season} to {output}")

if __name__ == "__main__":
    main()
