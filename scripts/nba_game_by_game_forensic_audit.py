"""Game-by-game forensic NBA ATS and totals audit.

Fits only on prior seasons. Emits one row for every matched holdout game,
with each pregame feature's additive contribution to projected margin/total.
It intentionally does not invent historical injury, player, travel or odds
price data that the source does not contain.
"""
from __future__ import annotations
import csv, json, math, urllib.request
from collections import defaultdict
from pathlib import Path
from src.api.nba_historical_backtest import _download, _games, FEATURES
from scripts.nba_reverse_engineer_formula import make_examples, fit

OUT=Path("backtest-output/game_by_game_forensic_audit")
OUT.mkdir(parents=True,exist_ok=True)
EDGE=2.5
URLS={
 2025:"https://huggingface.co/datasets/minmaxxxer/nba-closing-lines-ats-2024-25/resolve/main/nba_2024_25_closing_lines_ats.csv",
 2026:"https://huggingface.co/datasets/minmaxxxer/nba-closing-lines-ats-2025-26/resolve/main/nba_2025_26_closing_lines_ats.csv"}
ALIASES={"Atlanta Hawks":"ATL","Boston Celtics":"BOS","Brooklyn Nets":"BKN","Charlotte Hornets":"CHA","Chicago Bulls":"CHI","Cleveland Cavaliers":"CLE","Dallas Mavericks":"DAL","Denver Nuggets":"DEN","Detroit Pistons":"DET","Golden State Warriors":"GSW","Houston Rockets":"HOU","Indiana Pacers":"IND","Los Angeles Clippers":"LAC","LA Clippers":"LAC","Los Angeles Lakers":"LAL","LA Lakers":"LAL","Memphis Grizzlies":"MEM","Miami Heat":"MIA","Milwaukee Bucks":"MIL","Minnesota Timberwolves":"MIN","New Orleans Pelicans":"NOP","New York Knicks":"NYK","Oklahoma City Thunder":"OKC","Orlando Magic":"ORL","Philadelphia 76ers":"PHI","Philadelphia 76 ers":"PHI","Phoenix Suns":"PHX","Portland Trail Blazers":"POR","Sacramento Kings":"SAC","San Antonio Spurs":"SAS","Toronto Raptors":"TOR","Utah Jazz":"UTA","Washington Wizards":"WAS"}
def team(s):
 s=str(s).strip(); return ALIASES.get(s,s.upper().replace(" ",""))
def fetch(year):
 req=urllib.request.Request(URLS[year],headers={"User-Agent":"Mozilla/5.0"})
 with urllib.request.urlopen(req,timeout=60) as r:return list(csv.DictReader(r.read().decode("utf-8-sig").splitlines()))
def contribution_map(x,beta):
 return {name:round(float(a)*float(b),4) for name,a,b in zip(FEATURES,x,beta)}
def side_result(pick,actual):
 if pick=="PASS": return "PASS"
 if abs(actual)<1e-9:return "PUSH"
 return "WIN" if (pick=="HOME" and actual>0) or (pick=="AWAY" and actual<0) else "LOSS"
def total_result(pick,actual):
 if pick=="PASS":return "PASS"
 if abs(actual)<1e-9:return "PUSH"
 return "WIN" if (pick=="OVER" and actual>0) or (pick=="UNDER" and actual<0) else "LOSS"
def main():
 all_rows=[]; summaries=[]
 cache={}
 for year in (2024,2025,2026):
  p=_download(year)
  try: cache[year]=make_examples(_games(p))
  finally: Path(p).unlink(missing_ok=True)
 for holdout,train_years in ((2025,(2024,)),(2026,(2024,2025))):
  train=[r for y in train_years for r in cache[y]]
  test=cache[holdout]
  mb=fit(train,"actual_margin"); tb=fit(train,"actual_total")
  odds=fetch(holdout)
  lookup={(str(r["date"])[:10],team(r["home_team"]),team(r["away_team"])):r for r in test}
  matched=[]
  for q in odds:
   if q.get("season_type")!="regular":continue
   date=str(q["date_et"])[:10]
   ex=lookup.get((date,team(q["home_team"]),team(q["away_team"])))
   if ex is None:continue
   x=ex["features"]
   pred_margin=sum(a*b for a,b in zip(x,mb))
   pred_total=sum(a*b for a,b in zip(x,tb))
   spread=float(q["close_spread_home"]); total=float(q["close_total"])
   ats_edge=pred_margin+spread
   ou_edge=pred_total-total
   side="HOME" if ats_edge>=EDGE else "AWAY" if ats_edge<=-EDGE else "PASS"
   ou="OVER" if ou_edge>=EDGE else "UNDER" if ou_edge<=-EDGE else "PASS"
   actual_margin=float(ex["actual_margin"])
   actual_total=float(ex["actual_total"])
   actual_ats=actual_margin+spread
   actual_ou=actual_total-total
   mc=contribution_map(x,mb); tc=contribution_map(x,tb)
   def strongest(d):
    return sorted(d.items(),key=lambda z:abs(z[1]),reverse=True)[:4]
   row={
    "season":f"{holdout-1}-{str(holdout)[-2:]}","date":date,
    "matchup":f'{q["away_team"]} @ {q["home_team"]}',
    "home_team":q["home_team"],"away_team":q["away_team"],
    "home_spread":spread,"closing_total":total,
    "actual_home_margin":actual_margin,"actual_total":actual_total,
    "projected_home_margin":round(pred_margin,3),"home_ats_edge_points":round(ats_edge,3),
    "side_decision":side,"side_result":side_result(side,actual_ats),
    "actual_home_ats_margin":round(actual_ats,3),
    "projected_total":round(pred_total,3),"total_edge_points":round(ou_edge,3),
    "total_decision":ou,"total_result":total_result(ou,actual_ou),
    "actual_over_under_margin":round(actual_ou,3),
    "home_cover_prob_no_vig":q.get("close_home_cover_prob"),
    "over_prob_no_vig":q.get("close_over_prob"),
    "margin_feature_contributions_json":json.dumps(mc,separators=(",",":")),
    "total_feature_contributions_json":json.dumps(tc,separators=(",",":")),
    "top_margin_drivers_json":json.dumps(strongest(mc),separators=(",",":")),
    "top_total_drivers_json":json.dumps(strongest(tc),separators=(",",":")),
    "feature_values_json":json.dumps(dict(zip(FEATURES,x)),separators=(",",":")),
    "training_seasons":",".join(f"{y-1}-{str(y)[-2:]}" for y in train_years)
   }
   matched.append(row); all_rows.append(row)
  def stat(market,decision_key,result_key):
   bets=[r for r in matched if r[decision_key]!="PASS"]
   w=sum(r[result_key]=="WIN" for r in bets); l=sum(r[result_key]=="LOSS" for r in bets); psh=sum(r[result_key]=="PUSH" for r in bets)
   return {"bets":len(bets),"wins":w,"losses":l,"pushes":psh,
    "win_pct_ex_pushes":round(100*w/(w+l),2) if w+l else None,
    "units_at_minus_110":round(w*100/110-l,2)}
  summaries.append({"season":f"{holdout-1}-{str(holdout)[-2:]}","training_games":len(train),
   "matched_games":len(matched),"sides":stat("sides","side_decision","side_result"),
   "totals":stat("totals","total_decision","total_result"),
   "method":"Frozen prior-season ridge model; per-feature additive contributions; fixed +/-2.5 point edge rule"})
 fields=list(all_rows[0])
 with (OUT/"every_game.csv").open("w",newline="") as f:
  w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(all_rows)
 report={"title":"Every-game pregame factor audit","holdout_seasons":summaries,
  "feature_names":FEATURES,"fixed_edge_threshold_points":EDGE,
  "formula":"Projected home margin or total = sum(feature value x coefficient). ATS edge = projected home margin + closing home spread. Total edge = projected total - closing total.",
  "decision_rule":"HOME/AWAY or OVER/UNDER only when absolute point edge >= 2.5; otherwise PASS. No top-five truncation: every matched game is retained.",
  "data_limitations":["Historical closing consensus lines and no-vig probabilities are used; actual DraftKings prices are not available in this dataset.",
  "Historical injury/player availability, starting lineups, travel distance/time zones and altitude are not present in the feature source and are not fabricated.",
  "Rest days and back-to-back status are included. Feature contributions are model arithmetic, not causal effects.",
  "Totals use a separately trained projected-total regression and a fixed point-edge threshold; no total bet price is available, so results are not a priced ROI estimate."]}
 (OUT/"summary.json").write_text(json.dumps(report,indent=2))
 print(json.dumps(report,indent=2))
 print("AUDIT_ROWS",len(all_rows))
if __name__=="__main__":main()
