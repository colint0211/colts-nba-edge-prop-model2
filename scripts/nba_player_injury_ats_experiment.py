"""Leakage-safe player-injury ATS residual experiment; research only."""
from __future__ import annotations
import csv, json, math, re, urllib.request
from collections import defaultdict, deque
from datetime import date, datetime, time
from pathlib import Path
import duckdb
from scripts.nba_historical_matchup_backtest import LINES_URL, team_code, num

OUT=Path("backtest-output/player_injury_ats_experiment")
OUT.mkdir(parents=True,exist_ok=True)
BASE="https://github.com/sportsdataverse/sportsdataverse-data/releases/download"
IW=BASE+"/nba_stats_injury_report/injury_report_{s}.parquet"
PW=BASE+"/nba_stats_player_boxscores/player_boxscores_{s}.parquet"
WEIGHT={"Out":1.0,"Doubtful":0.9,"Questionable":0.5,"Probable":0.1,"Available":0.0}
INJURY_FEATURES=["net_missing_value","net_missing_minutes","net_missing_count","net_questionable_weight"]
MARKET_FEATURES=["spread_move_toward_home","home_win_probability_move","total_move"]
FEATURES=INJURY_FEATURES+MARKET_FEATURES

def get(url):
    q=urllib.request.Request(url,headers={"User-Agent":"ColtsNBAResearch/1.0"})
    with urllib.request.urlopen(q,timeout=120) as r:return r.read()

def mins(v):
    if v is None:return 0.0
    s=str(v).strip()
    if s.startswith("PT"):
        m=re.search(r"PT(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?",s)
        return float(m.group(1) or 0)+float(m.group(2) or 0)/60 if m else 0
    if ":" in s:
        try:
            a,b=s.split(":",1);return max(0,float(a)+float(b)/60)
        except:return 0
    try:return max(0,float(s))
    except:return 0

def gs(r):
    p,fm,fa,ftm,fta,oreb,dreb,ast,stl,blk,tov,pf=[float(x or 0) for x in r]
    return p+.4*fm-.7*fa-.4*(fta-ftm)+.7*oreb+.3*dreb+stl+.7*ast+.7*blk-.4*pf-tov

def tipoff(d,t):
    try:
        dd=date.fromisoformat(str(d)[:10]); hh,mm=str(t).replace("(ET)","").strip().split(":")[:2]
        return datetime.combine(dd,time(int(hh),int(mm)))
    except:return None

def lines_for(s):
    label=f"{s-1}-{str(s)[-2:]}"
    url=LINES_URL.format(season=label,season_underscore=label.replace("-","_"))
    q=urllib.request.Request(url,headers={"User-Agent":"ColtsNBAResearch/1.0"})
    with urllib.request.urlopen(q,timeout=120) as r:rows=list(csv.DictReader(r.read().decode("utf-8-sig").splitlines()))
    out={}
    for x in rows:
        if str(x.get("season_type","")).lower()!="regular":continue
        h,a=team_code(x.get("home_team")),team_code(x.get("away_team"));d=str(x.get("date_et",""))[:10]
        if h and a and d:out[(d,h,a)]={"date":d,"home":h,"away":a,"hs":num(x,"home_score"),"as":num(x,"away_score"),
                      "spread":num(x,"close_spread_home"),"open_spread":num(x,"open_spread_home"),
                      "open_home_win_prob":num(x,"open_home_win_prob"),"close_home_win_prob":num(x,"close_home_win_prob"),
                      "open_total":num(x,"open_total"),"close_total":num(x,"close_total")}
    return out

def load(s):
    ip=OUT/f"injury_{s}.parquet";pp=OUT/f"players_{s}.parquet"
    ip.write_bytes(get(IW.format(s=s)));pp.write_bytes(get(PW.format(s=s)))
    con=duckdb.connect()
    ir=con.execute("""SELECT report_datetime,game_date,game_time,game_id,home_team_abbr,away_team_abbr,team_id,person_id,current_status FROM read_parquet(?)""",[str(ip)]).fetchall()
    meta={};latest={}
    for dt,d,t,gid,h,a,tid,pid,status in ir:
        if pid is None:continue
        gid=str(gid)
        if gid not in meta:
            tp=tipoff(d,t)
            if tp:meta[gid]=(str(d)[:10],team_code(h),team_code(a),tp)
        if gid not in meta or dt is None or dt>meta[gid][3]:continue
        st=str(status or "")
        if st not in WEIGHT:continue
        key=(gid,str(pid));old=latest.get(key)
        if old is None or dt>old[0]:latest[key]=(dt,st,str(tid or ""))
    pr=con.execute("""SELECT CAST(game_id AS VARCHAR),CAST(person_id AS VARCHAR),CAST(team_id AS VARCHAR),side,minutes,
      points,field_goals_made,field_goals_attempted,free_throws_made,free_throws_attempted,
      rebounds_offensive,rebounds_defensive,assists,steals,blocks,turnovers,fouls_personal
      FROM read_parquet(?) WHERE person_id IS NOT NULL AND game_id IS NOT NULL
      ORDER BY CAST(game_id AS BIGINT)""",[str(pp)]).fetchall()
    bygame=defaultdict(list)
    for x in pr:
        gid,pid,tid,side,mn,*st=x
        m=mins(mn)
        if m<=0:continue
        bygame[gid].append({"pid":pid,"tid":tid,"side":str(side).lower(),"mins":m,"gs":gs(st)})
    lines=lines_for(s); gameinfo={}
    for gid,(d,h,a,tp) in meta.items():
        if h and a and (d,h,a) in lines:gameinfo[gid]=lines[(d,h,a)]
    histories=defaultdict(lambda:deque(maxlen=10)); data=[]; missing_ids=set()
    for gid in sorted(gameinfo,key=lambda x:int(x)):
        g=gameinfo[gid];rows=bygame.get(gid,[])
        side_map={r["tid"]:r["side"] for r in rows}
        rep={}
        for side in ("home","away"):
            roster=[]
            for (tid,pid),hist in histories.items():
                if tid not in side_map or side_map[tid]!=side or not hist:continue
                tm=sum(z[0] for z in hist)
                if tm>0:roster.append((tm/len(hist),36*sum(z[1] for z in hist)/tm))
            roster.sort(reverse=True)
            bench=[z[1] for z in roster[8:12]] or [z[1] for z in roster[max(0,len(roster)//2):]]
            rep[side]=sum(bench)/len(bench) if bench else 0.0
        impact={"home":0.0,"away":0.0};missmin={"home":0.0,"away":0.0};count={"home":0.0,"away":0.0};qcount={"home":0.0,"away":0.0}
        for (kgid,pid),(dt,status,tid) in latest.items():
            if kgid!=gid:continue
            w=WEIGHT[status]
            if w<=0:continue
            side=side_map.get(tid)
            if side not in ("home","away"):continue
            hist=histories.get((tid,pid))
            if not hist:missing_ids.add(pid);continue
            total=sum(z[0] for z in hist);n=len(hist)
            if total<=0:continue
            expmin=total/n;rate=36*sum(z[1] for z in hist)/total
            shrink=min(.9,n/8);rate=shrink*rate+(1-shrink)*rep[side]
            impact[side]+=w*expmin/36*(rate-rep[side]);missmin[side]+=w*expmin;count[side]+=w
            if status=="Questionable":qcount[side]+=w
        f={"net_missing_value":impact["away"]-impact["home"],
           "net_missing_minutes":missmin["away"]-missmin["home"],
           "net_missing_count":count["away"]-count["home"],
           "net_questionable_weight":qcount["away"]-qcount["home"],
           "spread_move_toward_home":g["open_spread"]-g["spread"],
           "home_win_probability_move":g["close_home_win_prob"]-g["open_home_win_prob"],
           "total_move":g["close_total"]-g["open_total"]}
        margin=g["hs"]-g["as"]
        data.append({"season":f"{s-1}-{str(s)[-2:]}","game_id":gid,"date":g["date"],"matchup":g["away"]+" @ "+g["home"],
                     "ats":margin+g["spread"],"features":f,"reports":sum(1 for k in latest if k[0]==gid)})
        for r in rows:histories[(r["tid"],r["pid"])].append((r["mins"],r["gs"]))
    # Global player identity coverage: injured players are not expected to appear in that same game's box score.
    ids={str(x[0]) for x in con.execute("SELECT DISTINCT person_id FROM read_parquet(?) WHERE person_id IS NOT NULL",[str(ip)]).fetchall()}
    pids={str(x[0]) for x in con.execute("SELECT DISTINCT person_id FROM read_parquet(?) WHERE person_id IS NOT NULL",[str(pp)]).fetchall()}
    cov={"season":f"{s-1}-{str(s)[-2:]}","injury_games":len(meta),"line_matched_games":len(gameinfo),
         "player_boxscore_games":len(bygame),"global_injury_player_ids":len(ids),
         "global_player_ids_found_in_boxscores":len(ids&pids),
         "global_player_id_match_pct":round(100*len(ids&pids)/len(ids),2) if ids else None,
         "games_with_pre_tip_status":sum(x["reports"]>0 for x in data),
         "games_with_nonzero_injury_features":sum(any(abs(v)>1e-9 for v in x["features"].values()) for x in data),
         "unavailable_players_without_prior_team_boxscore":len(missing_ids)}
    return data,cov

def solve(a,b):
    n=len(b);m=[list(a[i])+[b[i]] for i in range(n)]
    for c in range(n):
        p=max(range(c,n),key=lambda r:abs(m[r][c]));m[c],m[p]=m[p],m[c]
        if abs(m[c][c])<1e-10:m[c][c]+=1e-8
        z=m[c][c]
        for j in range(c,n+1):m[c][j]/=z
        for r in range(n):
            if r==c:continue
            z=m[r][c]
            for j in range(c,n+1):m[r][j]-=z*m[c][j]
    return [m[i][n] for i in range(n)]

def fit(rows,feature_names):
    means=[];scales=[]
    for f in feature_names:
        v=[r["features"][f] for r in rows]
        mu=sum(v)/len(v)
        sd=math.sqrt(sum((x-mu)**2 for x in v)/max(1,len(v)-1)) or 1.0
        means.append(mu);scales.append(sd)
    ym=sum(r["ats"] for r in rows)/len(rows)
    p=len(feature_names);a=[[0.0]*(p+1) for _ in range(p+1)];b=[0.0]*(p+1)
    for r in rows:
        x=[1.0]+[(r["features"][f]-means[i])/scales[i] for i,f in enumerate(feature_names)]
        y=r["ats"]-ym
        for i in range(p+1):
            b[i]+=x[i]*y
            for j in range(p+1):a[i][j]+=x[i]*x[j]
    for i in range(1,p+1):a[i][i]+=10.0
    return {"means":means,"scales":scales,"ym":ym,"beta":solve(a,b),"features":feature_names}

def pred(m,r):
    x=[1.0]+[(r["features"][f]-m["means"][i])/m["scales"][i] for i,f in enumerate(m["features"])]
    return m["ym"]+sum(x[i]*m["beta"][i] for i in range(len(x)))

def evaluate(train,test,feature_names):
    m=fit(train,feature_names)
    err=[r["ats"]-pred(m,r) for r in train]
    sig=math.sqrt(sum(x*x for x in err)/max(1,len(err)-1))
    scale=max(1.0,sig*math.sqrt(3.0)/math.pi)
    out={"train":train[0]["season"],"test":test[0]["season"],"train_games":len(train),
         "test_games":len(test),"train_residual_sd":round(sig,3),
         "features":feature_names,"thresholds":{}}
    for t in (.55,.60):
        w=l=0
        for r in test:
            p=1.0/(1.0+math.exp(-max(-30,min(30,pred(m,r)/scale))))
            if p>=t:
                if r["ats"]>0:w+=1
                elif r["ats"]<0:l+=1
            elif p<=1-t:
                if r["ats"]<0:w+=1
                elif r["ats"]>0:l+=1
        n=w+l;u=w*(100.0/110.0)-l
        out["thresholds"][str(t)]={"bets":n,"wins":w,"losses":l,
            "win_pct":round(100*w/n,2) if n else None,
            "units_at_minus_110":round(u,2),
            "roi_pct":round(100*u/n,2) if n else None}
    return out

def main():
    a,ca=load(2025);b,cb=load(2026)
    result={"experiment":"player_injury_ats_residual_v2",
      "timing":"Latest report at or before scheduled ET tipoff; player value from up to 10 prior appearances only.",
      "player_value":"Hollinger-style Game Score per 36 shrunk toward team historical rotation replacement.",
      "models":"Compare injury-only, opening-to-closing market-movement-only, and combined ridge models; train one season, test the other.",
      "caveat":"Closing spread already incorporates much injury information; this tests residual predictive value, not earlier-line timing.",
      "coverage":[ca,cb],"cross_season":[
        {"direction":"2024-25_to_2025-26","models":{
            "injury_only":evaluate(a,b,INJURY_FEATURES),
            "market_movement_only":evaluate(a,b,MARKET_FEATURES),
            "combined":evaluate(a,b,FEATURES)}},
        {"direction":"2025-26_to_2024-25","models":{
            "injury_only":evaluate(b,a,INJURY_FEATURES),
            "market_movement_only":evaluate(b,a,MARKET_FEATURES),
            "combined":evaluate(b,a,FEATURES)}}],
      "integrity":{"post_tipoff_reports_used":False,"current_game_boxscores_used_for_features":False,"live_model_modified":False,"production_deployed":False}}
    (OUT/"player_injury_ats_experiment.json").write_text(json.dumps(result,indent=2))
    print(json.dumps(result,indent=2))

if __name__=="__main__":main()
