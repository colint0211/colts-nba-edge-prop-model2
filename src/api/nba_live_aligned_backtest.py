"""Production-formula-aligned NBA game model walk-forward validation.

Uses the live model's opponent-adjusted ORtg/DRtg and blended pace equations,
with only information available before each game date. Calibration offsets are
fit on prior seasons only and evaluated on the untouched latest-season holdout.
Historical injury reports and DraftKings closing lines are not in this source.
"""
import json, math, os, tempfile
from collections import defaultdict, deque
from urllib.request import Request, urlopen
import duckdb

BASE = "https://github.com/ramirobentes/nba_data/releases/download/pbp"
SEASONS = (2025, 2026, 2027)
WINDOW = 10
SHRINK = 0.15
HOME_COURT = 2.0


def download(url, path):
    req = Request(url, headers={"User-Agent":"ColtsNBAEdge/2.0","Accept":"application/octet-stream"})
    with urlopen(req, timeout=45) as response, open(path,"wb") as out:
        total=0
        while True:
            chunk=response.read(1024*1024)
            if not chunk: break
            total += len(chunk)
            if total > 80_000_000: raise RuntimeError("PBP parquet exceeds 80 MB safety limit")
            out.write(chunk)
    return total


def team_rating(rows):
    if len(rows)<3: return None
    poss_for=sum(r[0] for r in rows); pts_for=sum(r[1] for r in rows)
    poss_against=sum(r[2] for r in rows); pts_against=sum(r[3] for r in rows)
    if min(poss_for,poss_against)<=0: return None
    return {"ortg":100*pts_for/poss_for,"drtg":100*pts_against/poss_against,
            "pace":(poss_for+poss_against)/(2*len(rows)),"games":len(rows)}


def predict(home,away,history,league):
    h=team_rating(list(history[home])); a=team_rating(list(history[away]))
    if not h or not a: return None
    pace=0.5*(h["pace"]+a["pace"])
    h_eff=0.5*(h["ortg"]+a["drtg"])
    a_eff=0.5*(a["ortg"]+h["drtg"])
    h_eff=(1-SHRINK)*h_eff+SHRINK*league["ortg"]
    a_eff=(1-SHRINK)*a_eff+SHRINK*league["ortg"]
    pace=(1-SHRINK)*pace+SHRINK*league["pace"]
    hp=h_eff*pace/100+HOME_COURT/2
    ap=a_eff*pace/100-HOME_COURT/2
    return {"home":hp,"away":ap,"margin":hp-ap,"total":hp+ap,
            "home_prior":h["games"],"away_prior":a["games"]}


def summarize(rows, margin_offset=0.0, total_offset=0.0, probability_sd=None):
    if not rows: return None
    n=len(rows)
    me=[(r["margin"]+margin_offset)-r["actual_margin"] for r in rows]
    te=[(r["total"]+total_offset)-r["actual_total"] for r in rows]
    margin_bias=sum(me)/n
    total_bias=sum(te)/n
    margin_rmse=math.sqrt(sum(x*x for x in me)/n)
    total_rmse=math.sqrt(sum(x*x for x in te)/n)
    margin_sd=math.sqrt(sum((x-margin_bias)**2 for x in me)/n)
    total_sd=math.sqrt(sum((x-total_bias)**2 for x in te)/n)
    sd=max(1.0,float(probability_sd or margin_sd))
    probs=[0.5*(1.0+math.erf((r["margin"]+margin_offset)/(sd*math.sqrt(2.0)))) for r in rows]
    outcomes=[1.0 if r["actual_margin"]>0 else 0.0 for r in rows]
    brier=sum((p-y)**2 for p,y in zip(probs,outcomes))/n
    log_loss=-sum(y*math.log(max(1e-12,min(1-1e-12,p)))+(1-y)*math.log(max(1e-12,min(1-1e-12,1-p))) for p,y in zip(probs,outcomes))/n
    return {"games":n,
      "margin_mae":round(sum(abs(x) for x in me)/n,3),
      "margin_rmse":round(margin_rmse,3),"margin_residual_sd":round(margin_sd,3),
      "total_mae":round(sum(abs(x) for x in te)/n,3),
      "total_rmse":round(total_rmse,3),"total_residual_sd":round(total_sd,3),
      "margin_bias":round(margin_bias,3),"total_bias":round(total_bias,3),
      "winner_accuracy_pct":round(100*sum(((r["margin"]+margin_offset)>0)==(r["actual_margin"]>0) for r in rows)/n,2),
      "winner_brier":round(brier,5),"winner_log_loss":round(log_loss,5),
      "probability_sd_points":round(sd,3)}


def run():
    games=[]; files={}
    with tempfile.TemporaryDirectory(prefix="nba-live-aligned-") as folder:
        con=duckdb.connect(":memory:")
        try:
            for season in SEASONS:
                path=os.path.join(folder,f"pbp_regular_{season}.parquet")
                files[str(season)]={"file":os.path.basename(path),"bytes":download(f"{BASE}/pbp_regular_{season}.parquet",path)}
                p=path.replace("'","''")
                rows=con.execute(f"""SELECT game_date,game_id,team_home,team_away,
                    MAX(hs),MAX(vs),MAX(poss_home),MAX(poss_away)
                    FROM read_parquet('{p}') WHERE season_type='regular'
                    GROUP BY game_date,game_id,team_home,team_away
                    HAVING MAX(hs)>0 AND MAX(vs)>0 AND MAX(poss_home)>0 AND MAX(poss_away)>0
                    ORDER BY game_date,game_id""").fetchall()
                for x in rows:
                    games.append({"date":str(x[0]),"season":season,"id":str(x[1]),
                      "home":str(x[2]),"away":str(x[3]),"hs":float(x[4]),"as":float(x[5]),
                      "hp":float(x[6]),"ap":float(x[7])})
        finally: con.close()
    games.sort(key=lambda x:(x["date"],x["id"]))
    history=defaultdict(lambda:deque(maxlen=WINDOW)); league_games=deque(maxlen=200); scored=[]
    i=0
    while i<len(games):
        day=games[i]["date"]; batch=[]
        while i<len(games) and games[i]["date"]==day: batch.append(games[i]); i+=1
        if league_games:
            poss=sum(g["hp"]+g["ap"] for g in league_games)
            pts=sum(g["hs"]+g["as"] for g in league_games)
            league={"ortg":100*pts/poss if poss else 112.0,
                    "pace":poss/(2*len(league_games))}
        else: league={"ortg":112.0,"pace":99.0}
        for g in batch:
            pred=predict(g["home"],g["away"],history,league)
            if pred:
                scored.append({"date":day,"season":g["season"],"game_id":g["id"],
                  "margin":pred["margin"],"total":pred["total"],
                  "actual_margin":g["hs"]-g["as"],"actual_total":g["hs"]+g["as"],
                  "home_prior":pred["home_prior"],"away_prior":pred["away_prior"]})
        for g in batch:
            history[g["home"]].append((g["hp"],g["hs"],g["ap"],g["as"]))
            history[g["away"]].append((g["ap"],g["as"],g["hp"],g["hs"]))
            league_games.append(g)
    train=[r for r in scored if r["season"] in (2025,2026)]
    hold=[r for r in scored if r["season"]==2027]
    if not train or not hold: raise RuntimeError("Insufficient train/holdout samples")
    # Estimate additive calibration offsets from training seasons only.
    margin_offset=sum(r["actual_margin"]-r["margin"] for r in train)/len(train)
    total_offset=sum(r["actual_total"]-r["total"] for r in train)/len(train)
    train_summary=summarize(train)
    train_margin_sd=train_summary["margin_residual_sd"]
    result={"success":True,"method":"Walk-forward replay of live opponent-adjusted ORtg/DRtg and blended pace projection equations; last-10 team games; 15% league shrinkage; +2 home court; same-day outcomes withheld.",
      "source":"ramirobentes/nba_data PBP parquet","files":files,
      "train_seasons":[2025,2026],"holdout_season":2027,
      "train_raw":train_summary,"holdout_raw":summarize(hold,probability_sd=train_margin_sd),
      "calibration_fit_on_train_only":{"margin_offset_points":round(margin_offset,3),"total_offset_points":round(total_offset,3),"margin_residual_sd_points":train_margin_sd},
      "holdout_after_train_calibration":summarize(hold,margin_offset,total_offset,probability_sd=train_margin_sd),
      "games_scored":len(scored),"date_range":{"first":scored[0]["date"],"last":scored[-1]["date"]},
      "limitations":["This validates the live projection equations using historical rolling team ratings, not the exact historical Basketball-Reference snapshot pipeline.",
        "Historical pregame injury reports and player on/off data are unavailable; injured-player games are not injury-adjusted.",
        "Historical DraftKings closing lines are unavailable; spread-cover calibration, total-line calibration, ROI and CLV cannot be claimed."]}
    with open("nba_live_aligned_backtest.json","w") as f: json.dump(result,f,indent=2)
    print("LIVE_ALIGNED_BACKTEST="+json.dumps(result,sort_keys=True))
    return result


if __name__=="__main__": run()
