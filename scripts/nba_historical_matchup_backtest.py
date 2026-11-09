"""Leakage-safe walk-forward backtest of the live quantitative matchup engine.

Uses only team game results from dates strictly before each slate. Daily Top-5
selection mirrors game_model._candidate/_qualified thresholds. Historical
closing prices are not supplied in the free consensus file, so ROI is reported
at a flat -110 assumption; selection edge uses the source's no-vig close probs.
"""
from __future__ import annotations
import csv, json, math, os, re, urllib.request
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from src.api.quant_game_engine import project_matchup

SEASONS = ("2024-25", "2025-26")
BOX_URL = "https://boxscorelab.com/downloads/{season}-team-game-results.csv"
LINES_URL = "https://huggingface.co/datasets/minmaxxxer/nba-closing-lines-ats-{season}/resolve/main/nba_{season_underscore}_closing_lines_ats.csv"
OUT = Path("backtest-output")
TEAM_NAMES = {
"Atlanta Hawks":"ATL","Boston Celtics":"BOS","Brooklyn Nets":"BKN","Charlotte Hornets":"CHA",
"Chicago Bulls":"CHI","Cleveland Cavaliers":"CLE","Dallas Mavericks":"DAL","Denver Nuggets":"DEN",
"Detroit Pistons":"DET","Golden State Warriors":"GSW","Houston Rockets":"HOU","Indiana Pacers":"IND",
"Los Angeles Clippers":"LAC","LA Clippers":"LAC","Los Angeles Lakers":"LAL","LA Lakers":"LAL",
"Memphis Grizzlies":"MEM","Miami Heat":"MIA","Milwaukee Bucks":"MIL","Minnesota Timberwolves":"MIN",
"New Orleans Pelicans":"NOP","New York Knicks":"NYK","Oklahoma City Thunder":"OKC","Orlando Magic":"ORL",
"Philadelphia 76ers":"PHI","Phoenix Suns":"PHX","Portland Trail Blazers":"POR","Sacramento Kings":"SAC",
"San Antonio Spurs":"SAS","Toronto Raptors":"TOR","Utah Jazz":"UTA","Washington Wizards":"WAS"
}
def download(url):
    req=urllib.request.Request(url,headers={"User-Agent":"ColtsNBAHistoricalResearch/1.0"})
    with urllib.request.urlopen(req,timeout=90) as r:
        return r.read().decode("utf-8-sig")
def rows(url):
    return list(csv.DictReader(download(url).splitlines()))
def num(r,k):
    try: return float(r[k])
    except (KeyError,TypeError,ValueError): return 0.0
def truth(v): return str(v).strip().lower() in ("true","1","yes")
def poss(r):
    # Standard NBA estimated possession formula used by the model's Tank01 fallback.
    return num(r,"field_goal_attempts") + .44*num(r,"free_throw_attempts") - num(r,"offensive_rebounds") + num(r,"turnovers")
def team_code(name):
    s=str(name or "").strip()
    if s in TEAM_NAMES: return TEAM_NAMES[s]
    up=s.upper()
    aliases={"GS":"GSW","NO":"NOP","NY":"NYK","SA":"SAS","PHO":"PHX","WSH":"WAS"}
    if up in aliases:return aliases[up]
    if up in set(TEAM_NAMES.values()):return up
    return None
def logistic_cdf(x,mean,scale):
    z=max(-35.0,min(35.0,(float(x)-float(mean))/float(scale)))
    return 1.0/(1.0+math.exp(-z))
def normal_cdf(x,mean,sd):
    return .5*(1+math.erf((x-mean)/(sd*math.sqrt(2))))
def normal_market_probs(mean,sd,line,over=True):
    integer=abs(float(line)-round(float(line)))<1e-9
    if over:
        win=1-normal_cdf(float(line)+(.5 if integer else 0),mean,sd)
    else:
        win=normal_cdf(float(line)-(.5 if integer else 0),mean,sd)
    push=(normal_cdf(float(line)+.5,mean,sd)-normal_cdf(float(line)-.5,mean,sd)) if integer else 0.0
    return max(0,min(1,win)),max(0,min(1,push))
def ev_at_minus_110(p,push=0):
    return p*(100/110)-max(0,1-p-push)
def load_team_games(season):
    raw=rows(BOX_URL.format(season=season))
    # Build paired game rows so each team's defensive rating uses opponent possessions.
    index={}
    for r in raw:
        d=str(r.get("game_date",""))[:10]
        t=team_code(r.get("team")); o=team_code(r.get("opponent"))
        if d and t and o:
            index[(d,t,o)]=r
    games_by_date=defaultdict(list)
    for (d,t,o),r in index.items():
        opp=index.get((d,o,t))
        if not opp: continue
        ownp=poss(r); oppp=poss(opp)
        if ownp<=0 or oppp<=0: continue
        games_by_date[d].append({
          "team":t,"opponent":o,"home":truth(r.get("home")),
          "points":num(r,"points"),"allowed":num(r,"opponent_points"),
          "poss":ownp,"opp_poss":oppp,"pace":(ownp+oppp)/2
        })
    return games_by_date,len(raw)
def build_ratings(history):
    teams={}
    for code,games in history.items():
        if not games: continue
        p=sum(g["points"] for g in games); ap=sum(g["allowed"] for g in games)
        poss_sum=sum(g["poss"] for g in games); oppposs=sum(g["opp_poss"] for g in games)
        if poss_sum<=0 or oppposs<=0: continue
        teams[code]={"ortg":100*p/poss_sum,"drtg":100*ap/oppposs,
          "pace":sum(g["pace"] for g in games)/len(games),"games":len(games)}
    return teams
def make_candidates(game,model):
    margin_sd=float(model["score_distribution"]["margin_sd"])
    total_sd=float(model["score_distribution"]["total_sd"])
    scale=float(model["score_distribution"]["moneyline_logistic_scale"])
    mm=float(model["projected_margin"]); tm=float(model["projected_total"])
    candidates=[]
    # Spread close is home handicap: negative means home favorite.
    hline=float(game["close_spread_home"]); aline=-hline
    for team,line,market_p,team_mean in (
      (game["home"],hline,float(game["close_home_cover_prob"]),mm),
      (game["away"],aline,1-float(game["close_home_cover_prob"]),-mm)):
        cover_mean=team_mean+line
        if abs(line-round(line))<1e-9:
            p=1-logistic_cdf(.5,cover_mean,scale)
            push=logistic_cdf(.5,cover_mean,scale)-logistic_cdf(-.5,cover_mean,scale)
        else:
            p=1-logistic_cdf(0,cover_mean,scale); push=0.0
        ev=ev_at_minus_110(p,push)
        edge=(p-market_p)*100
        if edge>=2.5 and ev*100>=1.5 and p>=.50:
            candidates.append({"market":"sides","selection":team,"line":line,"prob":p,
              "market_prob":market_p,"edge_pp":edge,"ev_pct":ev*100,"odds":-110,
              "date":game["date"],"matchup":game["away"]+" @ "+game["home"],
              "home":game["home"],"away":game["away"],"actual_margin":game["home_score"]-game["away_score"],
              "actual_total":game["home_score"]+game["away_score"],"close_spread_home":hline,"close_total":game["close_total"],
              "projected_margin":mm,"projected_total":tm})
    for side,market_p,over in (
      ("OVER",float(game["close_over_prob"]),True),
      ("UNDER",1-float(game["close_over_prob"]),False)):
        line=float(game["close_total"])
        p,push=normal_market_probs(tm,total_sd,line,over)
        ev=ev_at_minus_110(p,push); edge=(p-market_p)*100
        if edge>=2.5 and ev*100>=1.5 and p>=.50:
            candidates.append({"market":"totals","selection":side,"line":line,"prob":p,
              "market_prob":market_p,"edge_pp":edge,"ev_pct":ev*100,"odds":-110,
              "date":game["date"],"matchup":game["away"]+" @ "+game["home"],
              "home":game["home"],"away":game["away"],"actual_margin":game["home_score"]-game["away_score"],
              "actual_total":game["home_score"]+game["away_score"],"close_spread_home":hline,"close_total":line,
              "projected_margin":mm,"projected_total":tm})
    return candidates
def grade(p):
    if p["market"]=="sides":
        result=p["actual_margin"]+p["line"] if p["selection"]==p["home"] else -p["actual_margin"]+p["line"]
    else:
        result=p["actual_total"]-p["line"] if p["selection"]=="OVER" else p["line"]-p["actual_total"]
    return "PUSH" if abs(result)<1e-9 else ("WIN" if result>0 else "LOSS")
def summarize(picks):
    w=sum(grade(p)=="WIN" for p in picks); l=sum(grade(p)=="LOSS" for p in picks); pushes=sum(grade(p)=="PUSH" for p in picks)
    units=sum((100/110 if grade(p)=="WIN" else -1 if grade(p)=="LOSS" else 0) for p in picks)
    return {"bets":len(picks),"wins":w,"losses":l,"pushes":pushes,
      "win_pct":round(100*w/(w+l),2) if w+l else None,"units_at_minus_110":round(units,2),
      "roi_pct":round(units/len(picks)*100,2) if picks else None}
def run():
    OUT.mkdir(exist_ok=True)
    all_picks=[]; season_summary=[]; coverage=[]
    for season in SEASONS:
        season_u=season.replace("-","_")
        box,box_rows=load_team_games(season)
        line_url=LINES_URL.format(season=season,season_underscore=season_u)
        line_rows=rows(line_url)
        # Only regular-season games have matching team-game history and are used for the primary report.
        line_rows=[r for r in line_rows if str(r.get("season_type","")).lower()=="regular"]
        line_by_date=defaultdict(list)
        for r in line_rows:
            h=team_code(r.get("home_team")); a=team_code(r.get("away_team"))
            if not h or not a: continue
            d=str(r.get("date_et",""))[:10]
            line_by_date[d].append({"date":d,"home":h,"away":a,
              "home_score":num(r,"home_score"),"away_score":num(r,"away_score"),
              "close_spread_home":num(r,"close_spread_home"),"close_total":num(r,"close_total"),
              "close_home_cover_prob":num(r,"close_home_cover_prob"),
              "close_over_prob":num(r,"close_over_prob")})
        history=defaultdict(list); season_picks=[]; scored=0; no_ratings=0; no_match=0
        for day in sorted(line_by_date):
            # Forecast every game on a date before adding any same-date result to history.
            ratings=build_ratings(history)
            daily={"sides":[],"totals":[]}
            for g in line_by_date[day]:
                h=ratings.get(g["home"]); a=ratings.get(g["away"])
                if not h or not a:
                    no_ratings+=1; continue
                try:
                    model=project_matchup(home=h,away=a,all_teams=ratings,
                      data_quality="derived_early_season",home_court_points=2.0,
                      margin_residual_sd=14.81,total_residual_sd=19.06)
                except Exception:
                    no_ratings+=1; continue
                scored+=1
                for c in make_candidates(g,model):
                    daily[c["market"]].append(c)
            # Mirror the live slate: rank qualified candidates by EV and retain top five per market.
            for market in ("sides","totals"):
                selected=sorted(daily[market],key=lambda x:(x["ev_pct"],x["edge_pp"]),reverse=True)[:5]
                season_picks.extend(selected)
            # Add completed games from the whole date only after all forecasts have been made.
            for tgames in box.get(day,[]):
                history[tgames["team"]].append(tgames)
        # Verify that line data and box-score game history overlap.
        box_dates=set(box); line_dates=set(line_by_date)
        coverage.append({"season":season,"team_game_rows":box_rows,"line_rows_regular":len(line_rows),
          "line_games_with_ratings":scored,"days":len(line_by_date),"box_dates":len(box_dates),
          "unmatched_line_dates":len(line_dates-box_dates),"games_skipped_no_prior_ratings":no_ratings})
        season_summary.append({"season":season,"sides":summarize([p for p in season_picks if p["market"]=="sides"]),
          "totals":summarize([p for p in season_picks if p["market"]=="totals"]),
          "combined":summarize(season_picks)})
        all_picks.extend(season_picks)
    report={"model":"quant_game_engine.project_matchup + game_model._candidate/_qualified thresholds",
      "selection":"Top 5 qualified sides and Top 5 qualified totals per game date; no same-day result leakage",
      "data":"BoxScore Lab team-game results; Hugging Face consensus closing spread/total and no-vig probabilities",
      "pricing_assumption":"Historical consensus file contains no closing American prices; ROI uses flat -110, not historical book price.",
      "thresholds":{"edge_pp_min":2.5,"ev_pct_min":1.5,"model_probability_min":0.50,"max_picks_per_market_per_day":5},
      "season_summaries":season_summary,
      "combined":{"sides":summarize([p for p in all_picks if p["market"]=="sides"]),
        "totals":summarize([p for p in all_picks if p["market"]=="totals"]),"all":summarize(all_picks)},
      "coverage":coverage,
      "limitations":["Historical injuries, travel, altitude and player availability are not reconstructed; those live adjustments are zero in this replay.",
        "Model is the current quantitative matchup engine replayed on season-to-date team efficiency; it is not the former player-prop model.",
        "2023-24 closing-line file was not available from the same public dataset, so this first executable run covers 2024-25 and 2025-26 only.",
        "Flat -110 ROI is an assumption because source data provides no historical prices."]}
    (OUT/"summary.json").write_text(json.dumps(report,indent=2))
    with (OUT/"picks.csv").open("w",newline="") as f:
        fields=list(all_picks[0]) if all_picks else ["market","selection","date"]
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(all_picks)
    print(json.dumps(report,indent=2))
if __name__=="__main__": run()
