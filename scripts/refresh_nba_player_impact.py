#!/usr/bin/env python3
"""Build a validated Basketball-Reference player role/impact proxy snapshot.

BPM is a box-score-derived estimate, NOT on/off data or a causal injury effect.
The API combines BPM with minutes and a replacement-level baseline, and keeps
strict fail-closed behavior when player/team matching is ambiguous.
"""
from __future__ import annotations
import json, re, sys, time, unicodedata
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
import pandas as pd
import requests

BASE = "https://www.basketball-reference.com/leagues"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.basketball-reference.com/",
}
TEAM = {
 "ATL":"ATL","BOS":"BOS","BRK":"BKN","CHO":"CHA","CHI":"CHI","CLE":"CLE",
 "DAL":"DAL","DEN":"DEN","DET":"DET","GSW":"GSW","HOU":"HOU","IND":"IND",
 "LAC":"LAC","LAL":"LAL","MEM":"MEM","MIA":"MIA","MIL":"MIL","MIN":"MIN",
 "NOP":"NOP","NYK":"NYK","OKC":"OKC","ORL":"ORL","PHI":"PHI","PHO":"PHX",
 "POR":"POR","SAC":"SAC","SAS":"SAS","TOR":"TOR","UTA":"UTA","WAS":"WAS",
}
def season_code(year):
    start=year-1
    return start+1, f"{start}-{str(start+1)[-2:]}"
def tables(url):
    last=None
    for attempt in range(3):
        try:
            response=requests.get(url,headers=HEADERS,timeout=30); response.raise_for_status()
            html=response.text; fragments=[html]+re.findall(r"<!--(.*?)-->",html,re.S)
            found=[]
            for fragment in fragments:
                try: found.extend(pd.read_html(StringIO(fragment)))
                except (ValueError,ImportError): pass
            if found: return found
            raise RuntimeError(f"No HTML tables in {url}; bytes={len(html)}")
        except Exception as exc:
            last=exc
            if attempt<2: time.sleep(2**attempt)
    raise RuntimeError(f"Basketball-Reference fetch failed: {last}")
def flatten(frame):
    cols=[]
    for col in frame.columns:
        if isinstance(col,tuple):
            vals=[str(v).strip() for v in col if str(v).strip() and not str(v).startswith("Unnamed")]
            cols.append("_".join(vals))
        else: cols.append(str(col).strip())
    out=frame.copy(); out.columns=cols
    return out
def find(frames,required):
    for frame in frames:
        f=flatten(frame)
        if required.issubset(set(f.columns)): return f
    raise RuntimeError("Missing player table columns: "+", ".join(sorted(required)))
def number(value):
    if pd.isna(value): return None
    raw=str(value).strip().replace(",","")
    if raw in ("","—","-"): return None
    return float(raw)
def name_key(value):
    key=re.sub(r"[^a-z0-9]","",unicodedata.normalize("NFKD",str(value or "").casefold()).encode("ascii","ignore").decode("ascii"))
    return re.sub(r"(jr|sr|ii|iii|iv|v)$","",key)
def collect(year):
    per_url=f"{BASE}/NBA_{year}_per_game.html"
    adv_url=f"{BASE}/NBA_{year}_advanced.html"
    per=find(tables(per_url),{"Player","Team","G","MP"})
    adv=find(tables(adv_url),{"Player","Team","BPM"})
    per_map={}
    for _,r in per.iterrows():
        player=str(r.get("Player") or "").strip()
        raw_team=str(r.get("Team") or "").strip()
        team=TEAM.get(raw_team) or ("TOT" if re.fullmatch(r"\d+TM",raw_team) else None)
        key=(name_key(player),team)
        if not player or not team or not key[0]: continue
        gp=number(r.get("G")); mpg=number(r.get("MP"))
        if gp is None or mpg is None or gp<1 or not 0<=mpg<=48: continue
        # For a traded player, team-specific rows are preferred; ignore TOT.
        per_map[key]={"player":player,"team":team,"games":int(gp),"minutes_per_game":mpg}
    adv_map={}
    for _,r in adv.iterrows():
        player=str(r.get("Player") or "").strip()
        raw_team=str(r.get("Team") or "").strip()
        team=TEAM.get(raw_team) or ("TOT" if re.fullmatch(r"\d+TM",raw_team) else None)
        bpm=number(r.get("BPM"))
        if not player or not team or bpm is None or not -25<=bpm<=25: continue
        adv_map[(name_key(player),team)]=bpm
    # Basketball-Reference marks traded-player aggregate rows as TOT. Prefer
    # that single season aggregate when both per-game and advanced TOT rows exist;
    # otherwise keep the team-specific rows and do not manufacture an aggregate.
    tot_players={
        player_key for player_key, team in per_map
        if team=="TOT" and (player_key,"TOT") in adv_map
    }
    if tot_players:
        per_map={key:row for key,row in per_map.items() if key[0] not in tot_players or key[1]=="TOT"}
        adv_map={key:value for key,value in adv_map.items() if key[0] not in tot_players or key[1]=="TOT"}
    out={}
    for key,row in per_map.items():
        if key not in adv_map: continue
        row=dict(row); row["bpm"]=adv_map[key]
        out[key]=row
    if len(out)<250:
        raise RuntimeError(f"Player snapshot too small for {year}: {len(out)} matched player-team rows")
    return out,per_url,adv_url
def main():
    now=datetime.now(timezone.utc)
    current_year=now.year+1 if now.month>=7 else now.year
    current_season=f"{current_year-1}-{str(current_year)[-2:]}"
    prior,prior_per,prior_adv=collect(current_year-1)
    older,older_per,older_adv=collect(current_year-2)
    current,cur_per,cur_adv=collect(current_year)
    players={}
    # Prior-season BPM is a player trait proxy, not a team rating. When a player
    # changes teams, carry a prior baseline across teams only when the normalized
    # player identity is unique in the prior-season source. If multiple team rows
    # exist, combine them using games played as weights.
    def by_player(rows):
        grouped={}
        for (player_key, prior_team), prior_row in rows.items():
            grouped.setdefault(player_key, []).append(prior_row)
        return grouped

    prior_by_player=by_player(prior)
    older_by_player=by_player(older)

    for key in set(prior)|set(current)|set(older):
        old=prior.get(key)
        cur=current.get(key)
        if old is None:
            candidates=prior_by_player.get(key[0], [])
            if candidates:
                weights=[max(1, int(candidate.get("games") or 0)) for candidate in candidates]
                total_weight=sum(weights)
                old={
                    "player": candidates[0]["player"],
                    "team": candidates[0]["team"],
                    "games": sum(int(candidate.get("games") or 0) for candidate in candidates),
                    "minutes_per_game": sum(candidate["minutes_per_game"]*weight for candidate,weight in zip(candidates,weights))/total_weight,
                    "bpm": sum(candidate["bpm"]*weight for candidate,weight in zip(candidates,weights))/total_weight,
                    "prior_team_count": len(candidates),
                    "baseline_age_seasons": 1,
                }
        if old is None:
            candidates=older_by_player.get(key[0], [])
            if candidates:
                weights=[max(1, int(candidate.get("games") or 0)) for candidate in candidates]
                total_weight=sum(weights)
                old={
                    "player": candidates[0]["player"],
                    "team": candidates[0]["team"],
                    "games": sum(int(candidate.get("games") or 0) for candidate in candidates),
                    "minutes_per_game": sum(candidate["minutes_per_game"]*weight for candidate,weight in zip(candidates,weights))/total_weight,
                    "bpm": sum(candidate["bpm"]*weight for candidate,weight in zip(candidates,weights))/total_weight,
                    "prior_team_count": len(candidates),
                    "baseline_age_seasons": 2,
                }
        elif "baseline_age_seasons" not in old:
            old=dict(old)
            old["baseline_age_seasons"]=1
            old["prior_team_count"]=1
        row=cur or old
        if not row: continue
        baseline_age=int(old.get("baseline_age_seasons",1)) if old else 0
        if cur and old:
            n=max(0,cur["games"])
            # A two-season-old baseline gets less influence than last season.
            prior_strength=10.0 if baseline_age>=2 else 15.0
            current_weight=n/(n+prior_strength)
            bpm=current_weight*cur["bpm"]+(1-current_weight)*old["bpm"]
            mpg=cur["minutes_per_game"] if n>=5 else 0.65*cur["minutes_per_game"]+0.35*old["minutes_per_game"]
            games=cur["games"]
            source="current_and_two_year_prior_BPM_shrunk" if baseline_age>=2 else "current_and_prior_season_BPM_shrunk"
        elif cur:
            bpm=0.60*cur["bpm"]; mpg=cur["minutes_per_game"]; games=cur["games"]
            source="current_season_BPM_heavily_shrunk"
        elif baseline_age>=2:
            # Older baseline only: shrink toward replacement level and reduce
            # expected role minutes to reflect the extra season of uncertainty.
            bpm=0.50*old["bpm"]+0.50*(-2.0)
            mpg=0.80*old["minutes_per_game"]
            games=0
            source="two_year_prior_only_shrunk_to_replacement"
        else:
            bpm=old["bpm"]; mpg=old["minutes_per_game"]; games=old["games"]
            source="prior_season_only"
        if not (-12<=bpm<=12 and 0<=mpg<=48): continue
        identity=key[0]+"|"+key[1]
        players[identity]={
            "player":row["player"],"player_key":key[0],"team":key[1],
            "games_current_season":cur["games"] if cur else 0,
            "minutes_per_game":round(float(mpg),2),
            "bpm_raw_current":round(cur["bpm"],2) if cur else None,
            "bpm_prior":round(old["bpm"],2) if old else None,
            "bpm_shrunk":round(float(bpm),3),
            "impact_source":source,
            "prior_team_count":int(old.get("prior_team_count",1)) if old else 0,
            "baseline_age_seasons":baseline_age,
        }
    if len(players)<250: raise RuntimeError(f"Validated player impact output too small: {len(players)}")
    payload={
      "schema_version":1,
      "source":"Basketball-Reference per-game minutes + Advanced BPM",
      "method":"BPM is a box-score-derived proxy, not on/off. Estimated absence points = (shrunk BPM - replacement BPM) * MPG/48 * team pace/100; replacement BPM fixed at -2.0. Current BPM is shrunk toward a prior-season player baseline (15-game strength), or a two-year-old baseline (10-game strength) when last season is unavailable. Two-year-only BPM is shrunk 50% toward replacement level and minutes reduced 20%. Cross-team prior records are combined by games played.",
      "replacement_bpm":-2.0,
      "season":current_season,
      "prior_season":f"{current_year-2}-{str(current_year-1)[-2:]}",
      "older_season":f"{current_year-3}-{str(current_year-2)[-2:]}",
      "fetched_at":now.isoformat(),
      "player_count":len(players),
      "players":players,
      "source_urls":[cur_per,cur_adv,prior_per,prior_adv,older_per,older_adv],
    }
    path=Path(sys.argv[1] if len(sys.argv)>1 else "data/nba_player_impact.json")
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(payload,separators=(",",":"),allow_nan=False)+"\n",encoding="utf-8")
    print(f"Validated {len(players)} player-team records for {current_season}; wrote {path}")
if __name__=="__main__": main()
