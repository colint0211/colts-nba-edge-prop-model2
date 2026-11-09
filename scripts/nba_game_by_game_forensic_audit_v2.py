"""Validated-source game-by-game audit of the current NBA matchup engine.

Uses BoxScoreLab team-game box scores and estimated possessions (FGA + .44*FTA
- OREB + TO), matching nba_historical_matchup_backtest.py. Replays each slate
before adding same-day results. Outputs every matched game, all model factors,
model-vs-no-vig probability edges, actual results, and the actual Top-5 slate
selection. Historical injuries/player availability/travel are not fabricated.
"""
from __future__ import annotations
import csv,json,math
from collections import defaultdict
from pathlib import Path
from scripts.nba_historical_matchup_backtest import (
 load_team_games,build_ratings,team_code,truth,num,normal_cdf,normal_market_probs,
 ev_at_minus_110,TEAM_NAMES,LINES_URL
)
from src.api.quant_game_engine import project_matchup

OUT=Path("backtest-output/game_by_game_forensic_audit_v2")
OUT.mkdir(parents=True,exist_ok=True)
SEASONS=("2024-25","2025-26")
def download_rows(url):
 import urllib.request
 req=urllib.request.Request(url,headers={"User-Agent":"ColtsNBAForensicAudit/1.0"})
 with urllib.request.urlopen(req,timeout=90) as r:
  return list(csv.DictReader(r.read().decode("utf-8-sig").splitlines()))
def sigmoid(x):
 x=max(-35.0,min(35.0,float(x)))
 return 1.0/(1.0+math.exp(-x))
def logistic_cdf(x,mean,scale):
 return sigmoid((float(x)-float(mean))/float(scale))
def rest_days(history,day):
 """Full calendar days off; consecutive game dates produce zero rest days."""
 if not history:return None
 from datetime import date
 try:
  gap=(date.fromisoformat(day)-date.fromisoformat(str(history[-1]["date"])[:10])).days
  return max(0,gap-1) if gap>=1 else None
 except Exception:return None
def factor_breakdown(h,a,ratings,model):
 league_ortg=sum(v["ortg"] for v in ratings.values())/len(ratings)
 league_drtg=sum(v["drtg"] for v in ratings.values())/len(ratings)
 league_pace=sum(v["pace"] for v in ratings.values())/len(ratings)
 pace=model["projected_pace"]
 home_eff=model["home_expected_offensive_rating"]
 away_eff=model["away_expected_offensive_rating"]
 return {
  "home_offense_points_margin":round(.60*(h["ortg"]-league_ortg)*league_pace/100,3),
  "away_defense_points_margin":round(.40*(a["drtg"]-league_drtg)*league_pace/100,3),
  "away_offense_points_margin":round(-.60*(a["ortg"]-league_ortg)*league_pace/100,3),
  "home_defense_points_margin":round(-.40*(h["drtg"]-league_drtg)*league_pace/100,3),
  "home_court_points_margin":2.0,
  "pace_points_margin":round((pace-league_pace)*(home_eff-away_eff)/100,3),
  "home_offense_points_total":round(.60*(h["ortg"]-league_ortg)*league_pace/100,3),
  "away_defense_points_total":round(.40*(a["drtg"]-league_drtg)*league_pace/100,3),
  "away_offense_points_total":round(.60*(a["ortg"]-league_ortg)*league_pace/100,3),
  "home_defense_points_total":round(.40*(h["drtg"]-league_drtg)*league_pace/100,3),
  "home_court_points_total":2.0,
  "league_baseline_points_total":round(2*league_ortg*league_pace/100,3),
  "pace_points_total":round((pace-league_pace)*(home_eff+away_eff)/100,3),
  "league_off_rating":round(league_ortg,3),"league_def_rating":round(league_drtg,3),
  "league_pace":round(league_pace,3),"projected_pace":pace
 }
def candidates(g,m):
 out=[]
 mm=float(m["projected_margin"]); tm=float(m["projected_total"])
 scale=float(m["score_distribution"]["moneyline_logistic_scale"])
 msd=float(m["score_distribution"]["margin_sd"]); tsd=float(m["score_distribution"]["total_sd"])
 hline=float(g["close_spread_home"]); total=float(g["close_total"])
 hp=float(g["close_home_cover_prob"]); op=float(g["close_over_prob"])
 for side,line,market_p,mean in (("HOME",hline,hp,mm),("AWAY",-hline,1-hp,-mm)):
  cover_mean=mean+line
  if abs(line-round(line))<1e-9:
   p=1-logistic_cdf(.5,cover_mean,scale)
  else:p=1-logistic_cdf(0,cover_mean,scale)
  edge=(p-market_p)*100; ev=ev_at_minus_110(p)*100
  qualifies=edge>=4 and ev>=2 and p>=.5 and edge<=12 and ev<=20
  out.append({"market":"SIDE","selection":side,"model_probability_pct":round(p*100,2),
   "market_probability_pct":round(market_p*100,2),"edge_pp":round(edge,2),"ev_assumed_minus110_pct":round(ev,2),
   "qualifies":qualifies,"line":line})
 for side,market_p,over in (("OVER",op,True),("UNDER",1-op,False)):
  p,_=normal_market_probs(tm,tsd,total,over)
  edge=(p-market_p)*100; ev=ev_at_minus_110(p)*100
  qualifies=edge>=4 and ev>=2 and p>=.5 and edge<=12 and ev<=20
  out.append({"market":"TOTAL","selection":side,"model_probability_pct":round(p*100,2),
   "market_probability_pct":round(market_p*100,2),"edge_pp":round(edge,2),"ev_assumed_minus110_pct":round(ev,2),
   "qualifies":qualifies,"line":total})
 return out
def grade_side(sel,actual_home_margin,home_line):
 margin=actual_home_margin+home_line if sel=="HOME" else -actual_home_margin-home_line
 return "PUSH" if abs(margin)<1e-9 else "WIN" if margin>0 else "LOSS"
def grade_total(sel,actual_total,line):
 margin=actual_total-line if sel=="OVER" else line-actual_total
 return "PUSH" if abs(margin)<1e-9 else "WIN" if margin>0 else "LOSS"
def summarize(picks):
 w=sum(x["result"]=="WIN" for x in picks); l=sum(x["result"]=="LOSS" for x in picks); p=sum(x["result"]=="PUSH" for x in picks)
 units=w*100/110-l
 return {"bets":len(picks),"wins":w,"losses":l,"pushes":p,
  "win_pct_ex_pushes":round(100*w/(w+l),2) if w+l else None,
  "units_at_assumed_minus_110":round(units,2),"roi_pct_assumed_minus_110":round(100*units/len(picks),2) if picks else None}
def main():
 all_rows=[]; season_reports=[]; all_selected=[]
 for season in SEASONS:
  box,nbox=load_team_games(season)
  season_u=season.replace("-","_")
  lines=download_rows(LINES_URL.format(season=season,season_underscore=season_u))
  bydate=defaultdict(list)
  for r in lines:
   if str(r.get("season_type","")).lower()!="regular":continue
   h=team_code(r.get("home_team")); a=team_code(r.get("away_team"))
   if not h or not a:continue
   d=str(r.get("date_et",""))[:10]
   bydate[d].append({"date":d,"home":h,"away":a,"home_score":num(r,"home_score"),
    "away_score":num(r,"away_score"),"close_spread_home":num(r,"close_spread_home"),
    "close_total":num(r,"close_total"),"close_home_cover_prob":num(r,"close_home_cover_prob"),
    "close_over_prob":num(r,"close_over_prob")})
  history=defaultdict(list); season_rows=[]; side_picks=[]; total_picks=[]; scored=0; skipped=0
  for day in sorted(bydate):
   ratings=build_ratings(history)
   slate=[]
   for g in bydate[day]:
    h=ratings.get(g["home"]); a=ratings.get(g["away"])
    if not h or not a:skipped+=1;continue
    try:
     model=project_matchup(home=h,away=a,all_teams=ratings,data_quality="derived_early_season",
       home_court_points=2.0,margin_residual_sd=14.81,total_residual_sd=19.06)
    except Exception:skipped+=1;continue
    scored+=1
    factors=factor_breakdown(h,a,ratings,model)
    cand=candidates(g,model)
    for c in cand:
     c["date"]=day;c["matchup"]=g["away"]+" @ "+g["home"]
     c["actual_result"]=grade_side(c["selection"],g["home_score"]-g["away_score"],g["close_spread_home"]) if c["market"]=="SIDE" else grade_total(c["selection"],g["home_score"]+g["away_score"],g["close_total"])
     c["projected_margin"]=model["projected_margin"];c["projected_total"]=model["projected_total"]
     c["actual_home_margin"]=g["home_score"]-g["away_score"];c["actual_total"]=g["home_score"]+g["away_score"]
     c["close_spread_home"]=g["close_spread_home"];c["close_total"]=g["close_total"]
     c["factors"]=factors
    slate.append({"date":day,"matchup":g["away"]+" @ "+g["home"],"home":g["home"],"away":g["away"],
     "projected_home":model["projected_home"],"projected_away":model["projected_away"],
     "projected_margin":model["projected_margin"],"projected_total":model["projected_total"],
     "actual_home_margin":g["home_score"]-g["away_score"],"actual_total":g["home_score"]+g["away_score"],
     "home_spread":g["close_spread_home"],"closing_total":g["close_total"],
     "home_ats_edge_points":round(model["projected_margin"]+g["close_spread_home"],3),
     "total_edge_points":round(model["projected_total"]-g["close_total"],3),
     "home_rest_days":rest_days(history[g["home"]],day),"away_rest_days":rest_days(history[g["away"]],day),
     "home_team_ortg":h["ortg"],"home_team_drtg":h["drtg"],"home_team_pace":h["pace"],
     "away_team_ortg":a["ortg"],"away_team_drtg":a["drtg"],"away_team_pace":a["pace"],
     "model_margin_sd":model["score_distribution"]["margin_sd"],"model_total_sd":model["score_distribution"]["total_sd"],
     "home_cover_market_prob_no_vig":g["close_home_cover_prob"],"over_market_prob_no_vig":g["close_over_prob"],
     "factor_breakdown_json":json.dumps(factors,separators=(",",":")),
     "candidate_details_json":json.dumps(cand,separators=(",",":"))})
   # Same-date outcomes enter the rolling team form only after all forecasts.
   for s in slate:
    for c in json.loads(s["candidate_details_json"]):
     if not c["qualifies"]:continue
     pick={**c,"result":c["actual_result"]}
     (side_picks if c["market"]=="SIDE" else total_picks).append(pick)
   # Match live slate policy: top five qualified per market by EV, then edge.
   for market in ("SIDE","TOTAL"):
    ranked=sorted([c for s in slate for c in json.loads(s["candidate_details_json"]) if c["market"]==market and c["qualifies"]],
      key=lambda x:(x["ev_assumed_minus110_pct"],x["edge_pp"]),reverse=True)
    for rank,c in enumerate(ranked[:5],1):
     c["daily_top5_rank"]=rank;c["result"]=c["actual_result"];c["season"]=season;all_selected.append(c)
     if market=="SIDE": pass
   all_rows.extend(slate)
   # Add same-day results from source after forecasts.
   for tgames in box.get(day,[]):history[tgames["team"]].append(tgames)
  # all qualified vs actual top5
  season_reports.append({"season":season,"source_team_game_rows":nbox,"matched_games_scored":scored,"skipped_games_no_prior_ratings":skipped,
   "qualified_all_games_sides":summarize(side_picks),"qualified_all_games_totals":summarize(total_picks),
   "top5_daily_sides":summarize([x for x in all_selected if x["market"]=="SIDE" and x.get("season")==season]),
   "top5_daily_totals":summarize([x for x in all_selected if x["market"]=="TOTAL" and x.get("season")==season])})
 fields=list(all_rows[0])
 with (OUT/"every_game.csv").open("w",newline="") as f:
  w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(all_rows)
 with (OUT/"top5_daily_picks.csv").open("w",newline="") as f:
  fields2=list(all_selected[0]) if all_selected else []
  w=csv.DictWriter(f,fieldnames=fields2);w.writeheader();w.writerows(all_selected)
 report={"title":"Every-game current-model factor audit using verified team-game possession calculation",
  "seasons":season_reports,"game_rows":len(all_rows),
  "factors":["opponent-adjusted home offensive efficiency","opponent-adjusted away offensive efficiency","home defensive resistance","away defensive resistance","blended pace","2.0-point home-court adjustment","rest days recorded as diagnostic only"],
  "selection":"Exact live candidate thresholds for early-season data: >=4 percentage-point edge, >=2% assumed EV at -110, model probability >=50%, cap edge at 12pp and EV at 20%; then top five sides and top five totals per date.",
  "limitations":["Historical injuries, player availability, starting lineups, travel distance/time zones and altitude are not reconstructed; they are not fabricated.",
  "Rest days are shown but are not added to the current quant_game_engine projection in this historical replay.",
  "Consensus no-vig closing probabilities are used. Historical executable sportsbook prices are unavailable, so -110 is only an accounting assumption.",
  "The audit is research-only and does not alter live picks or deploy the app."]}
 (OUT/"summary.json").write_text(json.dumps(report,indent=2))
 print(json.dumps(report,indent=2))
if __name__=="__main__":main()
