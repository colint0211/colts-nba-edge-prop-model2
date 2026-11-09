"""Game-level NBA spread and total model.

The player-prop ledger is intentionally untouched. Game predictions use their own
immutable ledger and daily slate tables so archived prop results can never be
mixed with the new sides/totals record.
"""
import hashlib
import json
import math
import os
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

_ET = "America/New_York"
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
    params={
      "LeagueID":"00","Season":season,"SeasonType":"Regular Season","MeasureType":"Advanced",
      "PerMode":"PerGame","PlusMinus":"N","PaceAdjust":"N","Rank":"N","LastNGames":str(last_n),
      "Month":"0","OpponentTeamID":"0","Location":"","Outcome":"","DateFrom":"","DateTo":"",
      "SeasonSegment":"","VsConference":"","VsDivision":""
    }
    url="https://stats.nba.com/stats/leaguedashteamstats?"+urlencode(params)
    headers={"Referer":"https://www.nba.com/","Origin":"https://www.nba.com","x-nba-stats-origin":"stats","x-nba-stats-token":"true"}
    payload,_=_json(url,headers,18)
    sets=payload.get("resultSets") or payload.get("resultSet") or []
    if isinstance(sets,dict): sets=[sets]
    target=next((x for x in sets if isinstance(x,dict) and "headers" in x and "rowSet" in x),None)
    if not target: raise RuntimeError("NBA Stats did not return team advanced ratings.")
    headers=target["headers"]; rows=target["rowSet"]; out={}
    for row in rows:
        item=dict(zip(headers,row))
        code=_canonical(item.get("TEAM_ABBREVIATION"))
        if not code: continue
        out[code]={
          "team":code,"games":int(item.get("GP") or 0),
          "ortg":float(item.get("OFF_RATING")),"drtg":float(item.get("DEF_RATING")),
          "net":float(item.get("NET_RATING")),"pace":float(item.get("PACE")),
          "efg":float(item.get("EFG_PCT") or 0),"tov":float(item.get("TM_TOV_PCT") or 0),
          "oreb":float(item.get("OREB_PCT") or 0),"ts":float(item.get("TS_PCT") or 0)
        }
    if len(out)<28: raise RuntimeError("NBA Stats advanced team table is incomplete ("+str(len(out))+"/30).")
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
    # The NBA Stats endpoint has repeatedly timed out from the Vercel runtime.
    # Use it only when explicitly enabled; Tank01 is the authenticated fallback.
    if os.getenv("USE_NBA_STATS_TEAM_RATINGS","").strip().lower() in ("1","true","yes"):
        now=time.time()
        if not force and _TEAM_CACHE["data"] and now-_TEAM_CACHE["at"]<21600:
            return _TEAM_CACHE["data"]
        current_year=datetime.now(timezone.utc).year
        season_start=current_year if datetime.now(timezone.utc).month>=7 else current_year-1
        current=f"{season_start}-{str(season_start+1)[-2:]}"
        previous=f"{season_start-1}-{str(season_start)[-2:]}"
        current_stats=_nba_stats(current,0)
        prior_stats={}
        try: prior_stats=_nba_stats(previous,0)
        except Exception: pass
        recent={}
        try: recent=_nba_stats(current,10)
        except Exception: pass
        combined={}
        for team,cur in current_stats.items():
            prior=prior_stats.get(team); games=cur["games"]
            prior_weight=max(0.12,min(0.65,8.0/(games+8.0))) if prior else 0.0
            row={}
            for key in ("ortg","drtg","pace","efg","tov","oreb","ts"):
                base=cur[key]*(1-prior_weight)+(prior[key]*prior_weight if prior else 0)
                recent_row=recent.get(team)
                if recent_row and recent_row.get("games",0)>=4:
                    base=base*0.78+recent_row[key]*0.22
                row[key]=base
            row.update({"games":games,"net":row["ortg"]-row["drtg"],"prior_weight":prior_weight,
                        "recent_sample":recent.get(team,{}).get("games",0),"source":"NBA Stats"})
            combined[team]=row
        if len(combined)<28: raise RuntimeError("Insufficient NBA Stats team ratings.")
        result={"teams":combined,"season":current,"prior_season":previous,
          "source":"NBA Stats leaguedashteamstats advanced","data_quality":"advanced","fetched_at":datetime.now(timezone.utc).isoformat()}
        _TEAM_CACHE.update({"at":now,"data":result})
        return result
    return _tank01_ratings(force)


def _schedule(force=False):
    now=time.time()
    if not force and _SCHEDULE_CACHE["data"] and now-_SCHEDULE_CACHE["at"]<1800:
        return _SCHEDULE_CACHE["data"]
    from zoneinfo import ZoneInfo
    today=datetime.now(ZoneInfo(_ET)).date()
    start=(today-timedelta(days=12)).strftime("%Y%m%d")
    end=(today+timedelta(days=1)).strftime("%Y%m%d")
    url="https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?"+urlencode({"dates":start+"-"+end,"limit":500})
    payload,_=_json(url,timeout=15)
    games=[]
    for event in payload.get("events") or []:
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

def _injury_adjustment(team,injuries,season,game_date):
    from zoneinfo import ZoneInfo
    try:
        target=datetime.fromisoformat(str(game_date).replace("Z","+00:00")).astimezone(ZoneInfo(_ET)).date()
    except Exception: target=date.today()
    relevant=[]
    for item in injuries:
        if item.get("team")!=team: continue
        status=str(item.get("status") or "").lower()
        if status not in ("out","doubtful","questionable","day-to-day"): continue
        reason=str(item.get("reason") or "").lower()
        if "g league" in reason or "two-way" in reason or "on assignment" in reason: continue
        raw_date=str(item.get("game_date") or "")
        try: report_date=datetime.strptime(raw_date,"%m/%d/%Y").date()
        except Exception: report_date=target
        if report_date==target: relevant.append(item)
    details=[]
    unpriced=False
    for item in relevant:
        status=str(item.get("status") or "").lower()
        # Known OUT players are already reflected in the current-season team
        # performance sample when they have been absent. Uncertain availability
        # cannot be valued without licensed EPM/on-off, so block the game.
        uncertain=status in ("doubtful","questionable","day-to-day")
        unpriced=unpriced or uncertain
        details.append({"player":item.get("player"),"status":item.get("status"),
          "minutes":None,"estimated_team_points":0.0,
          "impact_data_available":not uncertain,
          "impact_source":"known OUT; team baseline" if status=="out" else "unpriced availability risk"})
    return {"points":0.0,"players":details,"unpriced_injury":unpriced,
      "source":"official injury report; uncertain rotation availability blocks game"}
 
 
def _team_adjustment(team,opp,game_date,schedule,injuries,season,is_home):
    current_venue=team if is_home else opp
    sit=_situational(team,game_date,schedule,opp,current_venue)
    inj=_injury_adjustment(team,injuries,season,game_date)
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
    home_adj=_team_adjustment(home,away,game.get("commence_time"),schedule,injuries,season,True)
    away_adj=_team_adjustment(away,home,game.get("commence_time"),schedule,injuries,season,False)
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
    edge_floor=4.0 if row.get("data_quality")=="derived_early_season" else 2.5
    ev_floor=2.0 if row.get("data_quality")=="derived_early_season" else 1.5
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
    schedule=_schedule(force=True); settled=0
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

def build_board(force=False):
    initialize()
    settlement={"checked":0,"settled":0}
    try: settlement=_settle_pending()
    except Exception: pass
    slate_date=_active_slate_date()
    # A frozen slate is immutable. Later line movement must not rewrite the official pick.
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
            for candidate in _candidate(game,"totals",model):
                if _qualified(candidate): base_totals.append(candidate)
        except Exception as exc:
            unavailable.append({"event_id":game.get("event_id"),"reason":type(exc).__name__})
    base_sides.sort(key=lambda x:(-x["ev_pct"],-x["edge_pp"]))
    base_totals.sort(key=lambda x:(-x["ev_pct"],-x["edge_pp"]))
    review_ids={x["event_id"] for x in base_sides[:10]+base_totals[:10]}
    review_games=[game_by_id[event_id] for event_id in review_ids if event_id in game_by_id]

    def review_game(game):
        home=_injury_adjustment(game["home"],injuries,ratings["season"],game["commence_time"])
        away=_injury_adjustment(game["away"],injuries,ratings["season"],game["commence_time"])
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
            unavailable.append({"event_id":event_id,"reason":"rotation-player injury impact could not be verified; no pick"})
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
        for candidate in _candidate(game,"totals",model):
            if _qualified(candidate): totals.append(candidate)

    sides.sort(key=lambda x:(-x["ev_pct"],-x["edge_pp"]))
    totals.sort(key=lambda x:(-x["ev_pct"],-x["edge_pp"]))
    side_picks,side_saved=_save_slate(sides,"sides",slate_date)
    total_picks,total_saved=_save_slate(totals,"totals",slate_date)
    return {"slate_date":slate_date,"frozen":True,"sides":side_picks,"totals":total_picks,
      "metrics":_metrics(),"settlement":settlement,"game_count":len(future_games),
      "source_status":{"odds":"PropLine DraftKings ("+odds_source+")","team_ratings":ratings["source"],
        "season":ratings["season"],"injuries":injury_meta.get("status","unavailable"),
        "player_impact":"True EPM/on-off not integrated; uncertain rotation availability blocks the game; known OUT players are disclosed but not individually valued",
        "reviewed_games":len(review_games),"prop_line_quota":quota,"saved_sides":side_saved,"saved_totals":total_saved,
        "unavailable_games":unavailable[:10]},
      "model_policy":"Team-efficiency ratings projection with explicit early-season uncertainty. Not yet a historically fitted regression. Market odds are used only for EV and no-vig comparison, never as the model probability.",
      "thresholds":{"minimum_edge_pp":4.0 if ratings.get("data_quality")=="derived_early_season" else 2.5,
        "minimum_ev_pct":2.0 if ratings.get("data_quality")=="derived_early_season" else 1.5,"minimum_probability":50}}
 
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
