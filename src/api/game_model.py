"""Game-level NBA spread and total model.

The player-prop ledger is intentionally untouched. Game predictions use their own
immutable ledger and daily slate tables so archived prop results can never be
mixed with the new sides/totals record.
"""
import hashlib
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

_ET = "America/New_York"
# Latest formula-aligned walk-forward validation showed material total-projection
# bias (+4.53 points on the untouched 2026-27 holdout). Keep totals out of the
# official ledger until a new, independently validated calibration passes.
_TOTALS_OFFICIAL_ENABLED = False
_PROP_CACHE = {"at": 0.0, "events": None, "quota": {}}
_TEAM_CACHE = {"at": 0.0, "data": None}
_SCHEDULE_CACHE = {"at": 0.0, "data": None}
_CACHE_TTL = 900
_TEAM_NAMES = {
    "ATL":"Atlanta Hawks","BOS":"Boston Celtics","BKN":"Brooklyn Nets","CHA":"Charlotte Hornets",
    "CHI":"Chicago Bulls","CLE":"Cleveland Cavaliers","DAL":"Dallas Mavericks","DEN":"Denver Nuggets",
    "DET":"Detroit Pistons","GSW":"Golden State Warriors","HOU":"Houston Rockets","IND":"Indiana Pacers",
    "LAC":"LA Clippers","LAL":"Los Angeles Lakers","MEM":"Memphis Grizzlies","MIA":"Miami Heat",
    "MIL":"Milwaukee Bucks","MIN":"Minnesota Timberwolves","NOP":"New Orleans Pelicans","NYK":"New York Knicks",
    "OKC":"Oklahoma City Thunder","ORL":"Orlando Magic","PHI":"Philadelphia 76ers","PHX":"Phoenix Suns",
    "POR":"Portland Trail Blazers","SAC":"Sacramento Kings","SAS":"San Antonio Spurs","TOR":"Toronto Raptors",
    "UTA":"Utah Jazz","WAS":"Washington Wizards"
}
# Arena coordinates, IANA timezone and approximate elevation in feet.
_VENUES = {
 "ATL":(33.7573,-84.3963,"America/New_York",1050),"BOS":(42.3662,-71.0621,"America/New_York",20),
 "BKN":(40.6826,-73.9754,"America/New_York",30),"CHA":(35.2251,-80.8392,"America/New_York",750),
 "CHI":(41.8807,-87.6742,"America/Chicago",600),"CLE":(41.4965,-81.6882,"America/New_York",650),
 "DAL":(32.7905,-96.8103,"America/Chicago",430),"DEN":(39.7487,-105.0077,"America/Denver",5280),
 "DET":(42.3411,-83.0553,"America/Detroit",600),"GSW":(37.7680,-122.3877,"America/Los_Angeles",10),
 "HOU":(29.7508,-95.3621,"America/Chicago",50),"IND":(39.7640,-86.1555,"America/Indiana/Indianapolis",715),
 "LAC":(34.0430,-118.2673,"America/Los_Angeles",300),"LAL":(34.0430,-118.2673,"America/Los_Angeles",300),
 "MEM":(35.1382,-90.0506,"America/Chicago",330),"MIA":(25.7814,-80.1870,"America/New_York",10),
 "MIL":(43.0451,-87.9172,"America/Chicago",620),"MIN":(44.9795,-93.2760,"America/Chicago",830),
 "NOP":(29.9490,-90.0821,"America/Chicago",10),"NYK":(40.7505,-73.9934,"America/New_York",50),
 "OKC":(35.4634,-97.5151,"America/Chicago",1200),"ORL":(28.5392,-81.3839,"America/New_York",100),
 "PHI":(39.9012,-75.1720,"America/New_York",40),"PHX":(33.4457,-112.0712,"America/Phoenix",1080),
 "POR":(45.5316,-122.6668,"America/Los_Angeles",50),"SAC":(38.5802,-121.4997,"America/Los_Angeles",30),
 "SAS":(29.4270,-98.4375,"America/Chicago",650),"TOR":(43.6435,-79.3791,"America/Toronto",250),
 "UTA":(40.7683,-111.9011,"America/Denver",4226),"WAS":(38.8981,-77.0209,"America/New_York",25)
}

def _connect():
    url = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not configured.")
    import psycopg
    return psycopg.connect(url, connect_timeout=8)

def initialize():
    with _connect() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS nba_game_predictions (
          id BIGSERIAL PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE,
          slate_date TEXT NOT NULL, market TEXT NOT NULL, event_id TEXT NOT NULL,
          matchup TEXT NOT NULL, home_team TEXT NOT NULL, away_team TEXT NOT NULL,
          selection TEXT NOT NULL, line DOUBLE PRECISION NOT NULL, odds INTEGER NOT NULL,
          model_probability DOUBLE PRECISION NOT NULL, market_probability DOUBLE PRECISION,
          projected_home DOUBLE PRECISION, projected_away DOUBLE PRECISION,
          projected_margin DOUBLE PRECISION, projected_total DOUBLE PRECISION,
          model_version TEXT NOT NULL, snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), settled_at TIMESTAMPTZ,
          actual_value DOUBLE PRECISION, outcome TEXT, profit_units DOUBLE PRECISION
        );
        CREATE INDEX IF NOT EXISTS nba_game_predictions_slate ON nba_game_predictions(slate_date,market);
        CREATE INDEX IF NOT EXISTS nba_game_predictions_pending ON nba_game_predictions(settled_at) WHERE settled_at IS NULL;
        CREATE TABLE IF NOT EXISTS nba_game_slates (
          market TEXT NOT NULL, slate_date TEXT NOT NULL, candidates JSONB NOT NULL DEFAULT '[]'::jsonb,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), PRIMARY KEY(market,slate_date)
        );
        """)

def _json(url, headers=None, timeout=14):
    req = Request(url, headers={"User-Agent":"ColtsNBAEdge/2.0","Accept":"application/json",**(headers or {})})
    with urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8","replace")), dict(response.headers.items())

def _canonical(value):
    raw = str(value or "").strip()
    up = raw.upper()
    aliases = {"NY":"NYK","GS":"GSW","WSH":"WAS","PHO":"PHX","NO":"NOP","SA":"SAS"}
    if up in _TEAM_NAMES: return up
    if up in aliases: return aliases[up]
    low = raw.lower()
    for code,name in _TEAM_NAMES.items():
        if low == name.lower() or low == name.lower().replace("la ","los angeles "):
            return code
    # PropLine outcome labels often use a two- or three-letter city code
    # followed by the mascot (for example "DAL Mavericks", "NO Pelicans").
    prefixed = {
      "ATL":"ATL","BOS":"BOS","BKN":"BKN","CHA":"CHA","CHI":"CHI","CLE":"CLE",
      "DAL":"DAL","DEN":"DEN","DET":"DET","GS":"GSW","GSW":"GSW","HOU":"HOU",
      "IND":"IND","LA":"LA","LAC":"LAC","LAL":"LAL","MEM":"MEM","MIA":"MIA",
      "MIL":"MIL","MIN":"MIN","NO":"NOP","NOP":"NOP","NY":"NYK","NYK":"NYK",
      "OKC":"OKC","ORL":"ORL","PHI":"PHI","PHO":"PHX","PHX":"PHX","POR":"POR",
      "SAC":"SAC","SA":"SAS","SAS":"SAS","TOR":"TOR","UTA":"UTA","WAS":"WAS","WSH":"WAS"
    }
    parts=up.split()
    if len(parts)>=2 and parts[0] in prefixed:
        code=prefixed[parts[0]]
        mascot=" ".join(parts[1:]).lower()
        if code=="LA":
            if "clipper" in mascot: return "LAC"
            if "laker" in mascot: return "LAL"
            return None
        if code=="LAC" and "clipper" in mascot: return "LAC"
        if code=="LAL" and "laker" in mascot: return "LAL"
        return code
    if low.startswith("los angeles clippers"): return "LAC"
    if low.startswith("los angeles lakers"): return "LAL"
    return None

def _american_probability(odds):
    try: n=float(odds)
    except (TypeError,ValueError): return None
    if n==0: return None
    return (-n)/(-n+100) if n<0 else 100/(n+100)

def _payout(odds):
    n=float(odds)
    return 100/abs(n) if n<0 else n/100

def _cdf(x,mean,sd):
    return 0.5*(1.0+math.erf((x-mean)/(sd*math.sqrt(2.0))))

def _normal_market_probs(mean,sd,line,over=True):
    integer = abs(float(line)-round(float(line))) < 1e-9
    if over:
        win = 1-_cdf(float(line)+(0.5 if integer else 0),mean,sd)
        push = (_cdf(float(line)+0.5,mean,sd)-_cdf(float(line)-0.5,mean,sd)) if integer else 0.0
    else:
        win = _cdf(float(line)-(0.5 if integer else 0),mean,sd)
        push = (_cdf(float(line)+0.5,mean,sd)-_cdf(float(line)-0.5,mean,sd)) if integer else 0.0
    return max(0.0,min(1.0,win)),max(0.0,min(1.0,push))

def _market_ev(prob,push,odds):
    loss=max(0.0,1.0-prob-push)
    return prob*_payout(odds)-loss

def _active_slate_date():
    from zoneinfo import ZoneInfo
    now=datetime.now(ZoneInfo(_ET))
    return (now.date()-timedelta(days=1) if now.hour<7 else now.date()).isoformat()

def _fetch_lines(force=False):
    now=time.time()
    if not force and _PROP_CACHE["events"] is not None and now-_PROP_CACHE["at"]<_CACHE_TTL:
        return _PROP_CACHE["events"],"cache",_PROP_CACHE["quota"]
    key=os.getenv("PROPLINE_API_KEY","").strip()
    if not key: raise RuntimeError("PROPLINE_API_KEY is missing.")
    params=urlencode({"markets":"spreads,totals","bookmakers":"draftkings","includeLinks":"true","max_age":300})
    payload,headers=_json("https://api.prop-line.com/v1/sports/basketball_nba/odds?"+params,{"X-API-Key":key},20)
    events=payload if isinstance(payload,list) else (payload.get("events") or payload.get("data") or payload.get("results") or [])
    if not isinstance(events,list): raise RuntimeError("PropLine did not return an event list.")
    quota={k:headers.get(v) for k,v in {"daily_limit":"X-Daily-Limit","daily_used":"X-Daily-Used","daily_remaining":"X-Daily-Remaining"}.items()}
    _PROP_CACHE.update({"at":now,"events":events,"quota":quota})
    return events,"live",quota

def _parse_game_lines(events):
    rows=[]
    for event in events:
        if not isinstance(event,dict): continue
        home=_canonical(event.get("home_team")); away=_canonical(event.get("away_team"))
        if not home or not away or home==away: continue
        event_id=str(event.get("id") or "")
        commence=str(event.get("commence_time") or event.get("start_time") or "")
        if not event_id: continue
        dk=next((b for b in (event.get("bookmakers") or []) if str(b.get("key") or "").lower()=="draftkings"),None)
        if not dk: continue
        spread_out=[]; total_out=[]
        for market in dk.get("markets") or []:
            if not isinstance(market,dict) or market.get("suspended_at"): continue
            if str(market.get("line_type") or "main").lower() not in ("","main"): continue
            if market.get("period") not in (None,"","full_game","game","full"): continue
            key=str(market.get("key") or "").lower()
            if key=="spreads":
                spread_out.extend(market.get("outcomes") or [])
            elif key=="totals" and market.get("team") in (None,""):
                total_out.extend(market.get("outcomes") or [])
        spreads={}
        for outcome in spread_out:
            team=_canonical(outcome.get("name"))
            try: point=float(outcome.get("point")); price=int(float(outcome.get("price")))
            except (TypeError,ValueError,OverflowError): continue
            if team in (home,away) and price and abs(price)>=100 and abs(price)<=10000:
                spreads[team]={"line":point,"odds":price}
        totals={}
        for outcome in total_out:
            side=str(outcome.get("name") or "").strip().lower()
            if side not in ("over","under"): continue
            try: point=float(outcome.get("point")); price=int(float(outcome.get("price")))
            except (TypeError,ValueError,OverflowError): continue
            if price and abs(price)>=100 and abs(price)<=10000:
                totals[side]={"line":point,"odds":price}
        if len(spreads)==2 and set(spreads)=={home,away} and set(totals)=={"over","under"}:
            if abs(spreads[home]["line"]+spreads[away]["line"])<=0.6 and abs(totals["over"]["line"]-totals["under"]["line"])<1e-6:
                rows.append({"event_id":event_id,"commence_time":commence,"home":home,"away":away,
                    "matchup":_TEAM_NAMES[away]+" @ "+_TEAM_NAMES[home],"spreads":spreads,"totals":totals,
                    "bookmaker":"DraftKings","bookmaker_link":dk.get("link")})
    return rows

def _nba_stats(season,last_n=0):
    """Read a validated official NBA Stats snapshot, then try the live API.

    GitHub Actions fetches a validated Basketball-Reference snapshot and
    commits it to the preview branch. NBA Stats remains a final live attempt,
    but is known to time out from hosted cloud egress.
    """
    snapshot_path=os.path.join(os.path.dirname(__file__),"..","..","data","nba_team_advanced.json")
    try:
        with open(snapshot_path,"r",encoding="utf-8") as handle:
            snapshot=json.load(handle)
        fetched=datetime.fromisoformat(str(snapshot.get("fetched_at","")).replace("Z","+00:00"))
        age=(datetime.now(timezone.utc)-fetched.astimezone(timezone.utc)).total_seconds()
        cached=snapshot.get("teams") or {}
        if (snapshot.get("source")=="Basketball-Reference NBA team ratings and advanced stats"
                and snapshot.get("season")==season and snapshot.get("team_count")==30
                and len(cached)==30 and 0<=age<=86400):
            required=set(_TEAM_NAMES)
            if set(cached)==required:
                return cached
    except (OSError,ValueError,TypeError,KeyError):
        pass

    from nba_api.stats.endpoints import leaguedashteamstats
    from nba_api.stats.library.http import NBAStatsHTTP
    from curl_cffi import requests as curl_requests

    headers={
        "Host":"stats.nba.com",
        "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept":"application/json, text/plain, */*",
        "Accept-Language":"en-US,en;q=0.9",
        "Referer":"https://www.nba.com/",
        "Origin":"https://www.nba.com",
        "Connection":"keep-alive",
        "x-nba-stats-origin":"stats",
        "x-nba-stats-token":"true"
    }
    session=curl_requests.Session(impersonate="chrome120", http_version="v1")
    NBAStatsHTTP.get_session=lambda self: session
    endpoint=leaguedashteamstats.LeagueDashTeamStats(
        season=season,
        season_type_all_star="Regular Season",
        measure_type_detailed_defense="Advanced",
        per_mode_detailed="PerGame",
        league_id_nullable="00",
        last_n_games=last_n,
        month=0,
        opponent_team_id=0,
        pace_adjust="N",
        plus_minus="N",
        rank="N",
        timeout=45,
        headers=headers
    )
    frames=endpoint.get_data_frames()
    if not frames:
        raise RuntimeError("nba_api returned no Advanced team stats data frame.")
    rows=frames[0].to_dict(orient="records")
    out={}
    for item in rows:
        code=_canonical(item.get("TEAM_ABBREVIATION"))
        if not code: continue
        try:
            games=int(item.get("GP") or 0)
            ortg=float(item.get("OFF_RATING"))
            drtg=float(item.get("DEF_RATING"))
            net=float(item.get("NET_RATING"))
            pace=float(item.get("PACE"))
        except (TypeError,ValueError):
            continue
        if not all(math.isfinite(v) for v in (ortg,drtg,net,pace)):
            continue
        out[code]={
          "team":code,"games":games,"ortg":ortg,"drtg":drtg,"net":net,"pace":pace,
          "efg":float(item.get("EFG_PCT") or 0),"tov":float(item.get("TM_TOV_PCT") or 0),
          "oreb":float(item.get("OREB_PCT") or 0),"ts":float(item.get("TS_PCT") or 0)
        }
    if len(out)!=30:
        raise RuntimeError("nba_api Advanced team table incomplete ("+str(len(out))+"/30).")
    return out

def _tank01_ratings(force=False):
    now=time.time()
    if not force and _TEAM_CACHE["data"] and now-_TEAM_CACHE["at"]<21600:
        return _TEAM_CACHE["data"]
    from . import index as app_index
    payload,headers=app_index._tank01_request("/getNBATeams",{"teamStats":"true"},timeout=18)
    body=payload.get("body")
    teams=body if isinstance(body,list) else ([v for v in body.values() if isinstance(v,dict)] if isinstance(body,dict) else [])
    raw={}
    def total(stats,key):
        try: return float((stats.get(key) or {}).get("Total"))
        except (TypeError,ValueError): return 0.0
    for team in teams:
        if not isinstance(team,dict): continue
        code=_canonical(team.get("teamAbv"))
        off=team.get("offensiveStats") or {}; deff=team.get("defensiveStats") or {}
        if not code or not off or not deff: continue
        try: games=min(int(float(off.get("gamesPlayed"))),int(float(deff.get("gamesPlayed"))))
        except (TypeError,ValueError): games=0
        if games<1: continue
        ofga,ofta,otov,oreb=total(off,"fga"),total(off,"fta"),total(off,"TOV"),total(off,"reb")
        dfga,dfta,dtov,dreb=total(deff,"fga"),total(deff,"fta"),total(deff,"TOV"),total(deff,"reb")
        oposs=ofga-0.25*oreb+otov+0.44*ofta
        dposs=dfga-0.25*dreb+dtov+0.44*dfta
        points=total(off,"pts") or float(team.get("ppg") or 0)
        allowed=total(deff,"pts") or float(team.get("oppg") or 0)
        if oposs<=0 or dposs<=0 or points<=0 or allowed<=0: continue
        fgm=total(off,"fgm"); tpm=total(off,"tptfgm")
        raw[code]={"games":games,"ortg":points/oposs*100.0,"drtg":allowed/dposs*100.0,
          "pace":(oposs+dposs)/2.0,"efg":(fgm+0.5*tpm)/ofga if ofga else 0,
          "tov":otov/oposs if oposs else 0,"oreb":0.25,
          "ts":points/(2*(ofga+0.44*ofta)) if ofga+0.44*ofta else 0}
    if len(raw)<28:
        raise RuntimeError("Tank01 team efficiency fallback is incomplete ("+str(len(raw))+"/30).")
    league={key:sum(row[key] for row in raw.values())/len(raw) for key in ("ortg","drtg","pace","efg","tov","oreb","ts")}
    combined={}
    for code,row in raw.items():
        weight=max(0.18,min(0.60,6.0/(row["games"]+6.0)))
        combined[code]={key:row[key]*(1-weight)+league[key]*weight for key in league}
        combined[code].update({"games":row["games"],"net":combined[code]["ortg"]-combined[code]["drtg"],
          "prior_weight":weight,"recent_sample":row["games"],"source":"Tank01 estimated possessions"})
    current_year=datetime.now(timezone.utc).year
    season_start=current_year if datetime.now(timezone.utc).month>=7 else current_year-1
    season=f"{season_start}-{str(season_start+1)[-2:]}"
    result={"teams":combined,"season":season,"prior_season":None,
      "source":"Tank01 current-season box-score averages; ORtg/DRtg/pace derived using estimated possessions (25% OREB proxy), shrunk toward league mean",
      "data_quality":"derived_early_season","fetched_at":datetime.now(timezone.utc).isoformat(),
      "rate_limit_remaining":headers.get("x-ratelimit-requests-remaining")}
    _TEAM_CACHE.update({"at":now,"data":result})
    return result


def _team_ratings(force=False):
    """Prefer a validated Basketball-Reference team ratings snapshot."""
    now=time.time()
    if not force and _TEAM_CACHE["data"] and now-_TEAM_CACHE["at"]<21600:
        return _TEAM_CACHE["data"]

    nba_error=None
    try:
        current_year=datetime.now(timezone.utc).year
        season_start=current_year if datetime.now(timezone.utc).month>=7 else current_year-1
        current=f"{season_start}-{str(season_start+1)[-2:]}"
        current_stats=_nba_stats(current,0)
        league={key:sum(row[key] for row in current_stats.values())/len(current_stats)
                for key in ("ortg","drtg","pace","efg","tov","oreb","ts")}
        combined={}
        for team,cur in current_stats.items():
            games=cur["games"]
            prior_weight=max(0.12,min(0.65,8.0/(games+8.0))) if games>0 else 0.65
            row={key:cur[key]*(1-prior_weight)+league[key]*prior_weight
                 for key in ("ortg","drtg","pace","efg","tov","oreb","ts")}
            row.update({"games":games,"net":row["ortg"]-row["drtg"],
                        "prior_weight":prior_weight,"recent_sample":0,
                        "source":"Basketball-Reference current-season team ratings"})
            combined[team]=row
        if len(combined)!=30:
            raise RuntimeError("Basketball-Reference team ratings incomplete ("+str(len(combined))+"/30).")
        result={"teams":combined,"season":current,"prior_season":None,
                "source":"Basketball-Reference NBA team ratings and advanced stats; current-season ratings conservatively shrunk toward league average",
                "data_quality":"basketball_reference","fetched_at":datetime.now(timezone.utc).isoformat(),
                "fallback_used":False,"primary_source":"Basketball-Reference"}
        _TEAM_CACHE.update({"at":now,"data":result})
        return result
    except Exception as exc:
        nba_error=type(exc).__name__+": "+str(exc)[:180]

    fallback=_tank01_ratings(force=True)
    fallback=dict(fallback)
    fallback["fallback_used"]=True
    fallback["primary_source"]="Basketball-Reference team ratings and advanced stats"
    fallback["fallback_reason"]=nba_error or "Basketball-Reference snapshot unavailable"
    _TEAM_CACHE.update({"at":now,"data":fallback})
    return fallback

def _schedule(force=False):
    now=time.time()
    if not force and _SCHEDULE_CACHE["data"] and now-_SCHEDULE_CACHE["at"]<60:
        return _SCHEDULE_CACHE["data"]
    from zoneinfo import ZoneInfo
    today=datetime.now(ZoneInfo(_ET)).date()
    days=[(today-timedelta(days=i)).strftime("%Y%m%d") for i in range(7)]
    def fetch_day(day):
        url="https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?"+urlencode({"dates":day})
        try:
            payload,_=_json(url,timeout=5)
            return payload.get("events") or []
        except Exception:
            return []
    events=[]
    with ThreadPoolExecutor(max_workers=7) as pool:
        for batch in pool.map(fetch_day,days):
            events.extend(batch)
    games=[]; seen=set()
    for event in events:
        if not isinstance(event,dict) or str(event.get("id") or "") in seen: continue
        seen.add(str(event.get("id") or ""))
        competitions=event.get("competitions") or []
        if not competitions: continue
        competitors=competitions[0].get("competitors") or []
        teams={}
        for comp in competitors:
            team=comp.get("team") or {}
            code=_canonical(team.get("abbreviation") or team.get("displayName"))
            if code: teams[code]=comp
        if len(teams)!=2: continue
        status=(event.get("status") or {}).get("type") or {}
        games.append({"id":str(event.get("id") or ""),"date":str(event.get("date") or ""),
          "home":next((k for k,v in teams.items() if v.get("homeAway")=="home"),None),
          "away":next((k for k,v in teams.items() if v.get("homeAway")=="away"),None),
          "completed":bool(status.get("completed")),"state":status.get("state"),
          "home_score":next((int(float(v.get("score") or 0)) for v in teams.values() if v.get("homeAway")=="home"),0),
          "away_score":next((int(float(v.get("score") or 0)) for v in teams.values() if v.get("homeAway")=="away"),0)})
    _SCHEDULE_CACHE.update({"at":now,"data":games})
    return games


def _haversine(a,b):
    import math
    lat1,lon1=a[0],a[1]; lat2,lon2=b[0],b[1]
    p=math.pi/180
    dlat=(lat2-lat1)*p; dlon=(lon2-lon1)*p
    h=math.sin(dlat/2)**2+math.cos(lat1*p)*math.cos(lat2*p)*math.sin(dlon/2)**2
    return 3958.8*2*math.asin(min(1,math.sqrt(h)))

def _situational(team,game_date,schedule,opponent,current_venue):
    from zoneinfo import ZoneInfo
    try:
        target_dt=datetime.fromisoformat(str(game_date).replace("Z","+00:00"))
        target=target_dt.astimezone(ZoneInfo(_ET)).date()
    except Exception:
        target_dt=datetime.now(timezone.utc); target=target_dt.date()
    current=_VENUES.get(current_venue)
    prior=[]
    for g in schedule:
        if not g.get("completed") or team not in (g.get("home"),g.get("away")): continue
        try: d=datetime.fromisoformat(g["date"].replace("Z","+00:00")).astimezone(ZoneInfo(_ET)).date()
        except Exception: continue
        if d<target: prior.append((d,g))
    prior.sort(key=lambda x:x[0],reverse=True)
    if not prior:
        return {"rest_days":2,"travel_miles":0,"tz_change":0,"altitude_change":0}
    last_date,last_game=prior[0]
    rest=max(0,(target-last_date).days-1)
    last_venue_code=team if last_game.get("home")==team else last_game.get("home")
    previous=_VENUES.get(last_venue_code)
    travel=_haversine(previous,current) if previous and current else 0.0
    tz_change=0.0
    if previous and current:
        try:
            prev_offset=target_dt.astimezone(ZoneInfo(previous[2])).utcoffset().total_seconds()/3600
            curr_offset=target_dt.astimezone(ZoneInfo(current[2])).utcoffset().total_seconds()/3600
            tz_change=curr_offset-prev_offset
        except Exception: pass
    home_venue=_VENUES.get(team)
    altitude_change=(current[3]-home_venue[3]) if current and home_venue else 0
    return {"rest_days":rest,"travel_miles":round(travel),"tz_change":round(tz_change,1),"altitude_change":altitude_change}

_PLAYER_IMPACT_CACHE = {"at": 0.0, "data": None}
_PLAYER_IMPACT_TTL = 1800

def _player_name_key(value):
    import re, unicodedata
    raw=str(value or "").strip()
    if "," in raw:
        parts=[part.strip() for part in raw.split(",") if part.strip()]
        suffixes={"jr","sr","ii","iii","iv","v"}
        if len(parts)==2:
            raw=parts[1]+" "+parts[0]
        elif len(parts)==3 and parts[1].casefold().replace(".","") in suffixes:
            raw=parts[2]+" "+parts[0]+" "+parts[1]
        elif len(parts)>=2:
            raw=" ".join(parts[1:]+parts[:1])
    normalized=unicodedata.normalize("NFKD",raw.casefold()).encode("ascii","ignore").decode("ascii")
    key=re.sub(r"[^a-z0-9]", "", normalized)
    return re.sub(r"(jr|sr|ii|iii|iv|v)$","",key)

def _load_player_impact_snapshot(season):
    """Load a fresh, season-matched B-Ref minutes/BPM snapshot; fail closed."""
    now=time.time()
    cached=_PLAYER_IMPACT_CACHE.get("data")
    if cached is not None and now-_PLAYER_IMPACT_CACHE.get("at",0.0)<_PLAYER_IMPACT_TTL:
        return cached
    path=os.path.join(os.path.dirname(__file__),"..","..","data","nba_player_impact.json")
    try:
        with open(path,"r",encoding="utf-8") as handle: payload=json.load(handle)
        fetched=datetime.fromisoformat(str(payload.get("fetched_at","")).replace("Z","+00:00"))
        if fetched.tzinfo is None: fetched=fetched.replace(tzinfo=timezone.utc)
        age=max(0.0,(datetime.now(timezone.utc)-fetched.astimezone(timezone.utc)).total_seconds())
        players=payload.get("players") or {}
        current_team_count=int(payload.get("current_season_team_count") or 0)
        current_table_complete=bool(payload.get("current_season_player_table_complete")) and current_team_count>=25
        valid=(payload.get("schema_version")==1 and payload.get("season")==season
            and payload.get("player_count")==len(players) and len(players)>=250 and age<=36*3600
            and current_table_complete)
        data={"available":bool(valid),"players":players if valid else {},
            "meta":{"source":payload.get("source"),"season":payload.get("season"),
            "fetched_at":payload.get("fetched_at"),"age_hours":round(age/3600,2),
            "player_count":len(players),"status":"fresh" if valid else "stale_or_invalid",
            "current_season_player_table_complete":current_table_complete,
            "current_season_team_count":current_team_count,
            "method":payload.get("method"),"replacement_bpm":payload.get("replacement_bpm",-2.0)}}
    except Exception as exc:
        data={"available":False,"players":{},"meta":{"status":"unavailable","error":type(exc).__name__}}
    _PLAYER_IMPACT_CACHE.update({"at":now,"data":data})
    return data

def _injury_adjustment(team,injuries,season,game_date,pace=100.0):
    from zoneinfo import ZoneInfo
    try:
        target=datetime.fromisoformat(str(game_date).replace("Z","+00:00")).astimezone(ZoneInfo(_ET)).date()
    except Exception: target=date.today()
    relevant=[]
    for item in injuries:
        if item.get("team")!=team: continue
        status=str(item.get("status") or "").casefold()
        if status not in ("out","doubtful","questionable","day-to-day"): continue
        reason=str(item.get("reason") or "").casefold()
        if "g league" in reason or "two-way" in reason or "on assignment" in reason: continue
        raw_date=str(item.get("game_date") or "")
        try: report_date=datetime.strptime(raw_date,"%m/%d/%Y").date()
        except Exception: report_date=target
        if report_date==target: relevant.append(item)
    snapshot=_load_player_impact_snapshot(season)
    players=snapshot.get("players") or {}
    replacement_bpm=float(snapshot.get("meta",{}).get("replacement_bpm",-2.0))
    details=[]; unpriced=False; points=0.0
    for item in relevant:
        status=str(item.get("status") or "").casefold()
        name=str(item.get("player") or "")
        name_key=_player_name_key(name)
        record=players.get(name_key+"|"+team)
        if record is None:
            # Some official reports append a suffix (e.g. "Butler III")
            # that Basketball-Reference omits. Permit only suffix-only
            # normalization after the exact full-name lookup fails.
            import re
            base_key=re.sub(r"(jr|sr|ii|iii|iv|v)$","",name_key)
            if base_key!=name_key:
                record=players.get(base_key+"|"+team)
                if record is None:
                    name_key=base_key
        # A verified player record from another team can be used only when the
        # normalized player identity has exactly one snapshot record. This
        # supports traded players with a prior-season baseline without guessing
        # between multiple team-season rows.
        if record is None:
            # Prefer an official Basketball-Reference traded-player aggregate
            # (TOT) when present; otherwise require a single cross-team match.
            total_record=players.get(name_key+"|TOT")
            if total_record is not None:
                record=total_record
            else:
                matches=[value for key,value in players.items() if key.startswith(name_key+"|")]
                if len(matches)==1:
                    record=matches[0]
        # Questionable/doubtful/day-to-day availability is probabilistic and
        # is not priced as a full absence. Keep the game out until confirmed.
        if status!="out":
            unpriced=True
            details.append({"player":name,"status":item.get("status"),"minutes":None,
              "estimated_team_points":None,"impact_data_available":False,
              "impact_source":"availability probability not calibrated; game withheld"})
            continue
        if not snapshot.get("available"):
            unpriced=True
            details.append({"player":name,"status":item.get("status"),"minutes":None,
              "estimated_team_points":None,"impact_data_available":False,
              "impact_source":"fresh complete current-season player table unavailable; game withheld"})
            continue
        if not record:
            meta=snapshot.get("meta") or {}
            if meta.get("current_season_player_table_complete") and int(meta.get("current_season_team_count") or 0)>=25:
                details.append({"player":name,"status":item.get("status"),"minutes":None,
                  "games_current_season":0,"estimated_team_points":0.0,"impact_data_available":True,
                  "impact_source":"no current-season player row in validated full-team table; zero incremental absence adjustment to current-season team ratings"})
                continue
            unpriced=True
            details.append({"player":name,"status":item.get("status"),"minutes":None,
              "estimated_team_points":None,"impact_data_available":False,
              "impact_source":"player impact record unavailable and current-season table completeness unverified; game withheld"})
            continue
        try:
            mpg=float(record["minutes_per_game"]); bpm=float(record["bpm_shrunk"])
            gp=int(record.get("games_current_season") or 0)
            if not (math.isfinite(mpg) and math.isfinite(bpm) and 0<=mpg<=48 and -12<=bpm<=12):
                raise ValueError("invalid player impact values")
            if gp==0:
                details.append({"player":name,"status":item.get("status"),
                  "minutes":round(mpg,2),"games_current_season":0,
                  "bpm_shrunk":round(bpm,3),"replacement_bpm":replacement_bpm,
                  "estimated_team_points":0.0,"impact_data_available":True,
                  "impact_source":"zero current-season appearances; no incremental absence adjustment to current-season team ratings"})
                continue
            # A tiny early-season sample is not enough to price a rotation player.
            # Permit only a clearly low-minute role (<=12 MPG) after at least
            # three appearances; the snapshot's heavy early-season BPM shrinkage
            # plus the minute cap keeps this proxy contribution small. Higher-use
            # players without a prior baseline remain a hard block.
            if gp<5 and record.get("bpm_prior") is None and not (gp>=3 and mpg<=12.0):
                raise ValueError("fewer than five current games and no same-team prior-season baseline; player is not a low-minute role")
            raw=(bpm-replacement_bpm)*(mpg/48.0)*(float(pace)/100.0)
            impact=max(-2.5,min(5.5,raw))
            if not math.isfinite(impact): raise ValueError("non-finite impact")
            points-=impact
            details.append({"player":name,"status":item.get("status"),
              "minutes":round(mpg,2),"games_current_season":gp,
              "bpm_shrunk":round(bpm,3),"replacement_bpm":replacement_bpm,
              "estimated_team_points":round(-impact,3),
              "impact_data_available":True,
              "impact_source":"Basketball-Reference minutes + shrunk BPM proxy (not on/off)",
              "impact_method":"(BPM - replacement BPM) × MPG/48 × team pace/100"})
        except (KeyError,TypeError,ValueError,OverflowError):
            unpriced=True
            details.append({"player":name,"status":item.get("status"),"minutes":None,
              "estimated_team_points":None,"impact_data_available":False,
              "impact_source":"invalid player impact record; game withheld"})
    return {"points":round(points,3),"players":details,"unpriced_injury":unpriced,
      "source":"official NBA injury report + Basketball-Reference minutes/BPM proxy",
      "player_impact_meta":snapshot.get("meta")}

def _team_adjustment(team,opp,game_date,schedule,injuries,season,is_home,pace=100.0):
    current_venue=team if is_home else opp
    sit=_situational(team,game_date,schedule,opp,current_venue)
    inj=_injury_adjustment(team,injuries,season,game_date,pace)
    rest=0.0
    if sit["rest_days"]==0: rest-=1.0
    elif sit["rest_days"]>=2: rest+=0.25
    travel_penalty=min(1.0,sit["travel_miles"]/2500.0)*0.65
    timezone_penalty=min(0.6,abs(sit["tz_change"])*0.15)
    elevation_penalty=max(0.0,sit["altitude_change"]/5000.0)*0.35
    return {"rest_days":sit["rest_days"],"travel_miles":sit["travel_miles"],
      "timezone_change_hours":sit["tz_change"],"altitude_change_feet":sit["altitude_change"],
      "rest_adjustment":rest,"travel_adjustment":-travel_penalty-timezone_penalty,
      "injury_adjustment":inj["points"],"injuries":inj["players"],"unpriced_injury":inj.get("unpriced_injury",False),
      "total_adjustment":rest-travel_penalty-timezone_penalty-elevation_penalty+inj["points"]}

def _score_game(game,ratings,schedule,injuries,season):
    home,away=game["home"],game["away"]
    h=ratings["teams"].get(home); a=ratings["teams"].get(away)
    if not h or not a: return None
    pace=(h["pace"]+a["pace"])/2.0
    home_eff=(h["ortg"]+a["drtg"])/2.0
    away_eff=(a["ortg"]+h["drtg"])/2.0
    home_adj=_team_adjustment(home,away,game.get("commence_time"),schedule,injuries,season,True,pace)
    away_adj=_team_adjustment(away,home,game.get("commence_time"),schedule,injuries,season,False,pace)
    home_points=home_eff*pace/100.0+2.0+home_adj["total_adjustment"]
    away_points=away_eff*pace/100.0+away_adj["total_adjustment"]
    return {"projected_home":round(home_points,2),"projected_away":round(away_points,2),
      "projected_margin":round(home_points-away_points,2),"projected_total":round(home_points+away_points,2),
      "projected_pace":round(pace,2),"home_adjustments":home_adj,"away_adjustments":away_adj,
      "injury_uncertainty":bool(home_adj.get("unpriced_injury") or away_adj.get("unpriced_injury")),
      "rating_source":ratings.get("source"),"data_quality":ratings.get("data_quality","advanced"),
      "ratings":{"home":h,"away":a}}

def _candidate(game,market,model):
    candidates=[]
    if market=="sides":
        mean=model["projected_margin"]; sd=13.5 if model.get("data_quality")=="derived_early_season" else 12.3
        for team in (game["home"],game["away"]):
            quote=game["spreads"][team]; line=quote["line"]; odds=quote["odds"]
            team_mean=mean if team==game["home"] else -mean
            if abs(line-round(line))<1e-9:
                win=1-_cdf(-line+0.5,team_mean,sd)
                push=_cdf(-line+0.5,team_mean,sd)-_cdf(-line-0.5,team_mean,sd)
            else:
                win=1-_cdf(-line,team_mean,sd); push=0.0
            market_raw=[_american_probability(game["spreads"][t]["odds"]) for t in (game["home"],game["away"])]
            selected_raw=_american_probability(odds)
            no_vig=selected_raw/sum(market_raw) if selected_raw and sum(market_raw)>0 else None
            ev=_market_ev(win,push,odds)
            candidates.append({"market":"sides","selection":team,"line":line,"odds":odds,
              "model_probability":win,"push_probability":push,"market_probability":no_vig,
              "edge_pp":(win-no_vig)*100 if no_vig is not None else None,"ev_pct":ev*100,
              "expected_margin_after_spread":round(team_mean+line,2)})
    else:
        mean=model["projected_total"]; sd=18.0 if model.get("data_quality")=="derived_early_season" else 17.0
        for side in ("over","under"):
            quote=game["totals"][side]; line=quote["line"]; odds=quote["odds"]
            win,push=_normal_market_probs(mean,sd,line,over=(side=="over"))
            other="under" if side=="over" else "over"
            raw=_american_probability(odds); other_raw=_american_probability(game["totals"][other]["odds"])
            no_vig=raw/(raw+other_raw) if raw is not None and other_raw is not None else None
            ev=_market_ev(win,push,odds)
            candidates.append({"market":"totals","selection":side.upper(),"line":line,"odds":odds,
              "model_probability":win,"push_probability":push,"market_probability":no_vig,
              "edge_pp":(win-no_vig)*100 if no_vig is not None else None,"ev_pct":ev*100,
              "projected_total_edge":round(mean-line,2)})
    for c in candidates:
        c.update({"event_id":game["event_id"],"matchup":game["matchup"],"home_team":game["home"],
          "away_team":game["away"],"commence_time":game.get("commence_time"),
          "selection_name":_TEAM_NAMES.get(c.get("selection"),c.get("selection")) if market=="sides" else c.get("selection"),
          "projected_home":model["projected_home"],"projected_away":model["projected_away"],
          "projected_margin":model["projected_margin"],"projected_total":model["projected_total"],
          "projected_pace":model["projected_pace"],"home_adjustments":model["home_adjustments"],
          "away_adjustments":model["away_adjustments"],"rating_source":model.get("rating_source","NBA Stats"),
          "data_quality":model.get("data_quality","advanced"),
          "model_version":"sides-totals-efficiency-v1","sportsbook":"DraftKings"})
    return candidates

def _qualified(row):
    derived=row.get("data_quality")=="derived_early_season"
    edge_floor=4.0 if derived else 2.5
    ev_floor=2.0 if derived else 1.5
    # Prevent uncalibrated early-season ratings from treating extreme
    # model/market disagreements as actionable value.
    if derived and (row.get("edge_pp",0)>12.0 or row.get("ev_pct",0)>20.0):
        return False
    return (row.get("market_probability") is not None and row.get("edge_pp") is not None
      and row["edge_pp"]>=edge_floor and row["ev_pct"]>=ev_floor and row["model_probability"]>=0.50)

def _save_slate(rows,market,slate_date):
    safe=[]
    for row in rows:
        if not _qualified(row): continue
        safe.append(row)
    safe=safe[:5]
    payload=json.dumps(safe,separators=(",",":"),allow_nan=False)
    with _connect() as conn:
        conn.execute("""INSERT INTO nba_game_slates(market,slate_date,candidates)
          VALUES(%s,%s,%s::jsonb) ON CONFLICT(market,slate_date) DO NOTHING""",(market,slate_date,payload))
        stored=conn.execute("SELECT candidates FROM nba_game_slates WHERE market=%s AND slate_date=%s",(market,slate_date)).fetchone()
        candidates=stored[0] if stored else []
        if isinstance(candidates,str): candidates=json.loads(candidates)
        saved=0
        for row in candidates:
            identity="|".join([market,slate_date,str(row.get("event_id")),str(row.get("selection")),str(row.get("line")),str(row.get("odds"))])
            fp=hashlib.sha256(identity.encode()).hexdigest()
            cursor=conn.execute("""INSERT INTO nba_game_predictions
              (fingerprint,slate_date,market,event_id,matchup,home_team,away_team,selection,line,odds,
               model_probability,market_probability,projected_home,projected_away,projected_margin,projected_total,
               model_version,snapshot)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
              ON CONFLICT(fingerprint) DO NOTHING""",
              (fp,slate_date,market,str(row.get("event_id")),row.get("matchup"),row.get("home_team"),row.get("away_team"),
               row.get("selection"),row.get("line"),row.get("odds"),row.get("model_probability"),row.get("market_probability"),
               row.get("projected_home"),row.get("projected_away"),row.get("projected_margin"),row.get("projected_total"),
               row.get("model_version"),json.dumps(row,separators=(",",":"),allow_nan=False)))
            saved+=cursor.rowcount
    return candidates,saved

def _metrics():
    with _connect() as conn:
        rows=conn.execute("""SELECT market,COUNT(*),COUNT(*) FILTER(WHERE outcome='WIN'),
          COUNT(*) FILTER(WHERE outcome='LOSS'),COUNT(*) FILTER(WHERE outcome='PUSH'),
          COALESCE(SUM(profit_units),0) FROM nba_game_predictions WHERE settled_at IS NOT NULL GROUP BY market""").fetchall()
        total=conn.execute("""SELECT COUNT(*),COUNT(*) FILTER(WHERE outcome='WIN'),
          COUNT(*) FILTER(WHERE outcome='LOSS'),COUNT(*) FILTER(WHERE outcome='PUSH'),
          COALESCE(SUM(profit_units),0) FROM nba_game_predictions WHERE settled_at IS NOT NULL""").fetchone()
    def pack(row):
        n,w,l,p,u=row
        return {"settled":n,"wins":w,"losses":l,"pushes":p,"win_rate_pct":round(w/(w+l)*100,1) if w+l else None,"net_units":round(float(u or 0),2)}
    return {"all_time":pack(total),"by_market":{r[0]:pack(r[1:]) for r in rows}}

def _settle_pending():
    with _connect() as conn:
        rows=conn.execute("""SELECT id,slate_date,event_id,market,selection,line,odds,home_team,away_team
          FROM nba_game_predictions WHERE settled_at IS NULL ORDER BY created_at LIMIT 300""").fetchall()
    if not rows: return {"checked":0,"settled":0}
    schedule=_schedule(force=False); settled=0
    for row in rows:
        pid,slate,event_id,market,selection,line,odds,home,away=row
        try: slate_day=date.fromisoformat(str(slate)[:10])
        except Exception: continue
        match=next((g for g in schedule if g.get("completed") and g.get("home")==home and g.get("away")==away
          and abs((datetime.fromisoformat(g["date"].replace("Z","+00:00")).date()-slate_day).days)<=1),None)
        if not match: continue
        if market=="sides":
            actual=(match["home_score"]-match["away_score"]) if selection==home else (match["away_score"]-match["home_score"])
            result=actual+float(line)
            outcome="PUSH" if abs(result)<1e-9 else ("WIN" if result>0 else "LOSS")
        else:
            actual=match["home_score"]+match["away_score"]
            outcome="PUSH" if abs(actual-float(line))<1e-9 else ("WIN" if (actual>line if selection=="OVER" else actual<line) else "LOSS")
        profit=0.0 if outcome=="PUSH" else (_payout(odds) if outcome=="WIN" else -1.0)
        with _connect() as conn:
            cur=conn.execute("""UPDATE nba_game_predictions SET actual_value=%s,outcome=%s,profit_units=%s,settled_at=NOW()
              WHERE id=%s AND settled_at IS NULL""",(actual,outcome,profit,pid))
            settled+=cur.rowcount
    return {"checked":len(rows),"settled":settled}

def build_board(force=False,preview_date=None):
    initialize()
    settlement={"checked":0,"settled":0}
    try: settlement=_settle_pending()
    except Exception: pass
    preview=bool(preview_date)
    slate_date=str(preview_date) if preview else _active_slate_date()
    try: date.fromisoformat(slate_date)
    except Exception: raise RuntimeError("Slate date must be YYYY-MM-DD.")
    # Frozen official slates are immutable. Preview dates calculate without saving.
    if not preview:
        with _connect() as conn:
            frozen={}
            for market in ("sides","totals"):
                item=conn.execute("SELECT candidates,created_at FROM nba_game_slates WHERE market=%s AND slate_date=%s",(market,slate_date)).fetchone()
                if item:
                    data=item[0] if isinstance(item[0],list) else json.loads(item[0] or "[]")
                    frozen[market]={"picks":data,"saved_at":item[1].isoformat() if item[1] else None}
        if len(frozen)==2:
            return {"slate_date":slate_date,"frozen":True,"sides":frozen["sides"]["picks"],"totals":frozen["totals"]["picks"],
              "metrics":_metrics(),"settlement":settlement,"source_status":{"odds":"frozen DraftKings snapshot","team_ratings":"frozen pregame snapshot"}}
    events,odds_source,quota=_fetch_lines(force)
    games=_parse_game_lines(events)
    if not games: raise RuntimeError("PropLine returned no upcoming NBA games with both DraftKings spread and full-game total quotes.")
    from zoneinfo import ZoneInfo
    now=datetime.now(timezone.utc)
    future_games=[]
    for game in games:
        try:
            start_time=datetime.fromisoformat(game["commence_time"].replace("Z","+00:00"))
            if start_time>now and start_time.astimezone(ZoneInfo(_ET)).date().isoformat()==slate_date:
                future_games.append(game)
        except Exception: continue
    if not future_games:
        if preview:
            return {"slate_date":slate_date,"preview":True,"frozen":False,"sides":[],"totals":[],
              "metrics":_metrics(),"settlement":settlement,"game_count":0,
              "source_status":{"odds":"PropLine DraftKings ("+odds_source+")","team_ratings":"not needed; no lines for preview date",
                "injuries":"not needed","prop_line_quota":quota},
              "model_policy":"No DraftKings lines were available for the requested preview date. Nothing was saved."}
        schedule_probe=_schedule(force=False)
        if not schedule_probe:
            raise RuntimeError("Recent ESPN schedule unavailable; refusing to freeze an empty slate.")
        has_scheduled_game=False
        for item in schedule_probe:
            try:
                game_day=datetime.fromisoformat(item["date"].replace("Z","+00:00")).astimezone(ZoneInfo(_ET)).date().isoformat()
                game_start=datetime.fromisoformat(item["date"].replace("Z","+00:00"))
                if game_day==slate_date and game_start>now and not item.get("completed"):
                    has_scheduled_game=True; break
            except Exception: continue
        if has_scheduled_game:
            return {"slate_date":slate_date,"frozen":False,"sides":[],"totals":[],
              "metrics":_metrics(),"settlement":settlement,"game_count":0,
              "source_status":{"odds":"DraftKings lines not posted yet for scheduled games","team_ratings":"waiting for complete odds",
                "injuries":"waiting for odds","prop_line_quota":quota},
              "model_policy":"Games are scheduled for this date, but DraftKings spread/total lines are not available yet. The board will retry and will not freeze an empty slate."}
        side_picks,side_saved=_save_slate([],"sides",slate_date)
        total_picks,total_saved=_save_slate([],"totals",slate_date)
        return {"slate_date":slate_date,"frozen":True,"sides":side_picks,"totals":total_picks,
          "metrics":_metrics(),"settlement":settlement,"game_count":0,
          "source_status":{"odds":"PropLine DraftKings ("+odds_source+")","team_ratings":"not needed; no remaining games on this slate date",
            "injuries":"not needed","prop_line_quota":quota,"saved_sides":side_saved,"saved_totals":total_saved},
          "model_policy":"No upcoming games remain on the active Eastern slate date. No picks were forced."}
    ratings=_team_ratings(force)
    schedule=_schedule(force)
    try:
        from . import index as app_index
        injury_feed=app_index._load_official_injury_feed()
        injury_meta=injury_feed.get("meta") or {}
        injuries=(injury_feed.get("records") or []) if injury_meta.get("status")=="fresh" else []
    except Exception:
        injuries=[]; injury_meta={"status":"unavailable"}
    if injury_meta.get("status")!="fresh":
        raise RuntimeError("Fresh official NBA injury report is unavailable; refusing to lock picks.")

    # Stage 1: score every game from team efficiency, pace and travel/rest features.
    # Stage 2: review injury negatives only for the strongest initial candidates.
    base_models={}; game_by_id={}; base_sides=[]; base_totals=[]; unavailable=[]
    for game in future_games:
        try:
            model=_score_game(game,ratings,schedule,[],ratings["season"])
            if not model: continue
            base_models[game["event_id"]]=model; game_by_id[game["event_id"]]=game
            for candidate in _candidate(game,"sides",model):
                if _qualified(candidate): base_sides.append(candidate)
                elif ratings.get("data_quality")=="derived_early_season" and (candidate.get("edge_pp",0)>12 or candidate.get("ev_pct",0)>20):
                    unavailable.append({"event_id":game.get("event_id"),"reason":"extreme early-season model/market gap withheld pending calibration"})
            if _TOTALS_OFFICIAL_ENABLED:
                for candidate in _candidate(game,"totals",model):
                    if _qualified(candidate): base_totals.append(candidate)
                    elif ratings.get("data_quality")=="derived_early_season" and (candidate.get("edge_pp",0)>12 or candidate.get("ev_pct",0)>20):
                        unavailable.append({"event_id":game.get("event_id"),"reason":"extreme early-season model/market gap withheld pending calibration"})
            else:
                unavailable.append({"event_id":game.get("event_id"),"reason":"game totals disabled: latest untouched holdout shows material projection bias; no totals are eligible for official picks"})
        except Exception as exc:
            unavailable.append({"event_id":game.get("event_id"),"reason":type(exc).__name__})
    base_sides.sort(key=lambda x:(-x["ev_pct"],-x["edge_pp"]))
    base_totals.sort(key=lambda x:(-x["ev_pct"],-x["edge_pp"]))
    # Injury adjustments can reorder the entire board. Review every game
    # before final ranking so a high-impact absence outside the initial top
    # ten cannot be missed.
    review_games=list(future_games)

    def review_game(game):
        home=_injury_adjustment(game["home"],injuries,ratings["season"],game["commence_time"],ratings["teams"][game["home"]]["pace"])
        away=_injury_adjustment(game["away"],injuries,ratings["season"],game["commence_time"],ratings["teams"][game["away"]]["pace"])
        return game["event_id"],home,away

    reviewed={}
    if review_games:
        with ThreadPoolExecutor(max_workers=5) as pool:
            for event_id,home_injury,away_injury in pool.map(review_game,review_games):
                reviewed[event_id]=(home_injury,away_injury)

    sides=[]; totals=[]
    for event_id,(home_injury,away_injury) in reviewed.items():
        game=game_by_id[event_id]
        model=dict(base_models[event_id])
        if home_injury.get("unpriced_injury") or away_injury.get("unpriced_injury"):
            unavailable.append({"event_id":event_id,"reason":"official report lists an unavailable player whose absence impact or availability probability is not validated; no pick"})
            continue
        home_adjustments=dict(model["home_adjustments"])
        away_adjustments=dict(model["away_adjustments"])
        home_adjustments.update({"injury_adjustment":home_injury["points"],"injuries":home_injury["players"],"unpriced_injury":False})
        away_adjustments.update({"injury_adjustment":away_injury["points"],"injuries":away_injury["players"],"unpriced_injury":False})
        home_adjustments["total_adjustment"]+=home_injury["points"]
        away_adjustments["total_adjustment"]+=away_injury["points"]
        model["projected_home"]=round(model["projected_home"]+home_injury["points"],2)
        model["projected_away"]=round(model["projected_away"]+away_injury["points"],2)
        model["projected_margin"]=round(model["projected_home"]-model["projected_away"],2)
        model["projected_total"]=round(model["projected_home"]+model["projected_away"],2)
        model["home_adjustments"]=home_adjustments; model["away_adjustments"]=away_adjustments
        for candidate in _candidate(game,"sides",model):
            if _qualified(candidate): sides.append(candidate)
        if _TOTALS_OFFICIAL_ENABLED:
            for candidate in _candidate(game,"totals",model):
                if _qualified(candidate): totals.append(candidate)

    sides.sort(key=lambda x:(-x["ev_pct"],-x["edge_pp"]))
    totals.sort(key=lambda x:(-x["ev_pct"],-x["edge_pp"]))
    unique_unavailable=[]; seen_unavailable=set()
    for item in unavailable:
        key=(str(item.get("event_id") or ""),str(item.get("reason") or ""))
        if key in seen_unavailable: continue
        seen_unavailable.add(key)
        row=dict(item)
        game=game_by_id.get(str(item.get("event_id") or ""))
        if game: row["matchup"]=_TEAM_NAMES.get(game["away"],game["away"])+" @ "+_TEAM_NAMES.get(game["home"],game["home"])
        unique_unavailable.append(row)
    if preview:
        side_picks=sides[:5]; total_picks=totals[:5]; side_saved=0; total_saved=0
    else:
        side_picks,side_saved=_save_slate(sides,"sides",slate_date)
        total_picks,total_saved=_save_slate(totals,"totals",slate_date)
    return {"slate_date":slate_date,"preview":preview,"frozen":not preview,"sides":side_picks,"totals":total_picks,
      "metrics":_metrics(),"settlement":settlement,"game_count":len(future_games),
      "source_status":{"odds":"PropLine DraftKings ("+odds_source+")","team_ratings":ratings["source"],
        "season":ratings["season"],"team_rating_quality":ratings.get("data_quality"),"team_rating_fallback_used":ratings.get("fallback_used",False),"team_rating_fallback_reason":ratings.get("fallback_reason"),"team_count":len(ratings.get("teams") or {}),"injuries":injury_meta.get("status","unavailable"),
        "player_impact":"Basketball-Reference minutes + shrunk BPM replacement-level proxy (not on/off); OUT players priced only with fresh exact-team, official TOT, or unique cross-team player matches; questionable/doubtful/day-to-day remain blocked pending calibrated availability probabilities",
        "player_impact_snapshot":_load_player_impact_snapshot(ratings["season"]).get("meta"),
        "totals_validation":{"official_picks_enabled":_TOTALS_OFFICIAL_ENABLED,"status":"disabled_pending_holdout_bias_recalibration","holdout_total_bias_points":4.529},
        "reviewed_games":len(review_games),
        "injury_impact_audit":[{"event_id":event_id,"matchup":game_by_id[event_id]["matchup"],
          "home":{"team":game_by_id[event_id]["home"],"points":home.get("points"),"unpriced":home.get("unpriced_injury"),"players":home.get("players",[])},
          "away":{"team":game_by_id[event_id]["away"],"points":away.get("points"),"unpriced":away.get("unpriced_injury"),"players":away.get("players",[])}}
          for event_id,(home,away) in reviewed.items()],
        "prop_line_quota":quota,"saved_sides":side_saved,"saved_totals":total_saved,
        "unavailable_games":unique_unavailable[:10]},
      "model_policy":"Team-efficiency ratings projection with explicit early-season uncertainty. Not yet a historically fitted regression. Market odds are used only for EV and no-vig comparison, never as the model probability. Official game totals are disabled because the untouched 2026-27 holdout showed +4.53 points of total bias; sides remain separately eligible subject to injury and edge gates.",
      "thresholds":{"minimum_edge_pp":4.0 if ratings.get("data_quality")=="derived_early_season" else 2.5,
        "maximum_edge_pp":12.0 if ratings.get("data_quality")=="derived_early_season" else None,
        "minimum_ev_pct":2.0 if ratings.get("data_quality")=="derived_early_season" else 1.5,
        "maximum_ev_pct":20.0 if ratings.get("data_quality")=="derived_early_season" else None,"minimum_probability":50}}
 
def history(slate_date):
    initialize()
    with _connect() as conn:
        rows=conn.execute("""SELECT id,market,matchup,selection,line,odds,model_probability,market_probability,
          projected_home,projected_away,projected_margin,projected_total,actual_value,outcome,profit_units,created_at
          FROM nba_game_predictions WHERE slate_date=%s ORDER BY market,id""",(str(slate_date),)).fetchall()
    return [{"id":r[0],"market":r[1],"matchup":r[2],"selection":r[3],"line":r[4],"odds":r[5],
      "model_probability_pct":round(r[6]*100,2),"market_probability_pct":round(r[7]*100,2) if r[7] is not None else None,
      "projected_home":r[8],"projected_away":r[9],"projected_margin":r[10],"projected_total":r[11],
      "actual_value":r[12],"outcome":r[13],"profit_units":r[14],"created_at":r[15].isoformat() if r[15] else None} for r in rows]
