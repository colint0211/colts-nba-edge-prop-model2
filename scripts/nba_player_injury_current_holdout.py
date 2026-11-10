"""Apply a frozen historical injury+market model to untouched 2026-27 games.

Current-season injury statuses are timestamped and limited to reports published
by tipoff. Player minutes/impact are estimated only from 2025-26 box scores
because the current season is still early. The model is not deployed.
"""
from __future__ import annotations
import csv,json,math,urllib.request
from collections import defaultdict,deque
from datetime import date,datetime,time
from pathlib import Path
import duckdb
from scripts import nba_player_injury_ats_experiment as hist

OUT=Path("backtest-output/player_injury_current_holdout")
OUT.mkdir(parents=True,exist_ok=True)
BASE="https://github.com/sportsdataverse/sportsdataverse-data/releases/download"
INJ=BASE+"/nba_stats_injury_report/injury_report_2027.parquet"
PLAY=BASE+"/nba_stats_player_boxscores/player_boxscores_2026.parquet"
ODDS="https://huggingface.co/datasets/minmaxxxer/nba-odds-2026-27/resolve/main/nba_2026_27_odds.csv"
W={"Out":1.0,"Doubtful":.9,"Questionable":.5,"Probable":.1,"Available":0.0}

def parse_tipoff(d,t):
 try:
  dd=date.fromisoformat(str(d)[:10]);hh,mm=str(t).replace("(ET)","").strip().split(":")[:2]
  return datetime.combine(dd,time(int(hh),int(mm)))
 except:return None

def get(url):
 q=urllib.request.Request(url,headers={"User-Agent":"ColtsNBAResearch/1.0"})
 with urllib.request.urlopen(q,timeout=120) as r:return r.read()

def main():
 ip=OUT/"injury_2027.parquet";pp=OUT/"players_2026_prior.parquet"
 ip.write_bytes(get(INJ));pp.write_bytes(get(PLAY))
 con=duckdb.connect()
 ir=con.execute("""SELECT report_datetime,game_date,game_time,game_id,home_team_abbr,away_team_abbr,team_id,person_id,current_status FROM read_parquet(?)""",[str(ip)]).fetchall()
 meta={};latest={}
 for dt,d,t,gid,h,a,tid,pid,status in ir:
  gid=str(gid)
  if gid not in meta:
   tp=parse_tipoff(d,t)
   if tp:meta[gid]=(str(d)[:10],str(h),str(a),hist.team_code(h),hist.team_code(a),tp)
  if pid is None or gid not in meta or dt is None or dt>meta[gid][5]:continue
  st=str(status or "")
  if st not in W:continue
  key=(gid,str(pid));old=latest.get(key)
  if old is None or dt>old[0]:latest[key]=(dt,st,str(tid or ""))

 # Prior-season player histories and team rotation baselines.
 rows=con.execute("""SELECT CAST(game_id AS VARCHAR),CAST(person_id AS VARCHAR),CAST(team_id AS VARCHAR),
  team_tricode,minutes,points,field_goals_made,field_goals_attempted,free_throws_made,free_throws_attempted,
  rebounds_offensive,rebounds_defensive,assists,steals,blocks,turnovers,fouls_personal
  FROM read_parquet(?) WHERE person_id IS NOT NULL AND game_id IS NOT NULL
  ORDER BY CAST(game_id AS BIGINT)""",[str(pp)]).fetchall()
 ph=defaultdict(lambda:deque(maxlen=10));th=defaultdict(lambda:defaultdict(lambda:deque(maxlen=10)));idcode={}
 for x in rows:
  gid,pid,tid,tri,mn,*stats=x;m=hist.mins(mn)
  if not tid or not tri:idcode[str(tid)]=str(tri or "")
  if m<=0:continue
  rec=(m,hist.gs([float(v or 0) for v in stats]))
  ph[str(pid)].append(rec);th[str(tid)][str(pid)].append(rec);idcode[str(tid)]=str(tri)

 rep={}
 for tid,roster in th.items():
  vals=[]
  for pid,h in roster.items():
   tm=sum(x[0] for x in h)
   if tm>0:vals.append((tm/len(h),36*sum(x[1] for x in h)/tm))
  vals.sort(reverse=True);bench=[x[1] for x in vals[8:12]] or [x[1] for x in vals[max(0,len(vals)//2):]]
  if bench:rep[tid]=sum(bench)/len(bench)
 league_rep=sum(rep.values())/len(rep) if rep else 0.0

 # Current odds file.
 req=urllib.request.Request(ODDS,headers={"User-Agent":"ColtsNBAResearch/1.0"})
 with urllib.request.urlopen(req,timeout=120) as r:odds=list(csv.DictReader(r.read().decode("utf-8-sig").splitlines()))
 lines={}
 for x in odds:
  if str(x.get("season_type","")).lower()!="regular":continue
  h,a=hist.team_code(x.get("home_team")),hist.team_code(x.get("away_team"));d=str(x.get("date_et",""))[:10]
  if h and a and d:
   try:
    lines[(d,h,a)]={"date":d,"home":h,"away":a,"hs":float(x["home_score"]),"as":float(x["away_score"]),
      "spread":float(x["close_spread_home"]),"open_spread":float(x["open_spread_home"]),
      "open_home_win_prob":float(x["open_home_win_prob"]),"close_home_win_prob":float(x["close_home_win_prob"]),
      "open_total":float(x["open_total"]),"close_total":float(x["close_total"])}
   except:pass

 test=[];no_line=0;unknown_ids=set()
 for gid,(d,ha,aa,h,a,tp) in meta.items():
  if not h or not a or (d,h,a) not in lines:continue
  g=lines[(d,h,a)]
  imp={"home":0.0,"away":0.0};mm={"home":0.0,"away":0.0};cnt={"home":0.0,"away":0.0};qcnt={"home":0.0,"away":0.0}
  for (kgid,pid),(dt,status,tid) in latest.items():
   if kgid!=gid:continue
   wt=W[status]
   if wt<=0:continue
   side="home" if idcode.get(tid)==h else "away" if idcode.get(tid)==a else None
   if side is None:continue
   phist=ph.get(pid)
   if not phist:
    unknown_ids.add(pid);continue
   tm=sum(x[0] for x in phist)
   if tm<=0:continue
   n=len(phist);expected=tm/n;rate=36*sum(x[1] for x in phist)/tm
   replacement=rep.get(tid,league_rep)
   shrink=min(.9,n/8);rate=shrink*rate+(1-shrink)*replacement
   imp[side]+=wt*expected/36*(rate-replacement);mm[side]+=wt*expected;cnt[side]+=wt
   if status=="Questionable":qcnt[side]+=wt
  feats={"net_missing_value":imp["away"]-imp["home"],
    "net_missing_minutes":mm["away"]-mm["home"],
    "net_missing_count":cnt["away"]-cnt["home"],
    "net_questionable_weight":qcnt["away"]-qcnt["home"],
    "spread_move_toward_home":g["open_spread"]-g["spread"],
    "home_win_probability_move":g["close_home_win_prob"]-g["open_home_win_prob"],
    "total_move":g["close_total"]-g["open_total"]}
  test.append({"season":"2026-27","game_id":gid,"date":d,"matchup":a+" @ "+h,
    "ats":g["hs"]-g["as"]+g["spread"],"features":feats})
  if not any(abs(v)>1e-9 for v in feats.values()):no_line+=1

 train_a,_=hist.load(2025);train_b,_=hist.load(2026);train=train_a+train_b
 models={}
 for name,features in (("injury_only",hist.INJURY_FEATURES),("market_movement_only",hist.MARKET_FEATURES),("combined",hist.FEATURES)):
  model=hist.fit(train,features)
  err=[r["ats"]-hist.pred(model,r) for r in train]
  sigma=math.sqrt(sum(e*e for e in err)/max(1,len(err)-1));scale=max(1,sigma*math.sqrt(3)/math.pi)
  out={"features":features,"train_residual_sd":round(sigma,3),"thresholds":{}}
  for t in (.55,.60):
   w=l=0;by={"HOME":[0,0],"AWAY":[0,0]}
   for r in test:
    p=1/(1+math.exp(-max(-30,min(30,hist.pred(model,r)/scale))))
    side="HOME" if p>=t else "AWAY" if p<=1-t else None
    if not side or abs(r["ats"])<1e-9:continue
    win=(r["ats"]>0) if side=="HOME" else (r["ats"]<0)
    by[side][0 if win else 1]+=1
    if win:w+=1
    else:l+=1
   n=w+l;u=w*100/110-l
   out["thresholds"][str(t)]={"bets":n,"wins":w,"losses":l,"win_pct":round(100*w/n,2) if n else None,
    "units_at_minus_110":round(u,2),"roi_pct":round(100*u/n,2) if n else None,
    "home_wins_losses":by["HOME"],"away_wins_losses":by["AWAY"]}
  models[name]=out
 result={"experiment":"frozen_2026_27_player_injury_market_holdout_v1",
  "training":"2024-25 + 2025-26; coefficients and thresholds frozen before 2026-27 evaluation.",
  "current_player_baseline":"2025-26 player box scores only; player rates/minutes from last 10 prior appearances, team replacement from prior-season rotation.",
  "injury_timing":"latest official report_datetime at or before listed game_time ET.",
  "coverage":{"current_odds_games":len(lines),"injury_games":len(meta),"line_matched_games":len(test),
    "games_with_pre_tip_report":sum(1 for gid in meta if any(k[0]==gid for k in latest)),
    "injury_player_ids_without_prior_history":len(unknown_ids)},
  "models":models,
  "integrity":{"test_season_in_training":False,"post_tipoff_reports_used":False,
    "current_game_player_stats_used_for_features":False,"live_model_modified":False,"production_deployed":False}}
 (OUT/"player_injury_current_holdout.json").write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2))

if __name__=="__main__":main()
