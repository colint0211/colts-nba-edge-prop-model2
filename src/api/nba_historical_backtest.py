"""Leakage-safe NBA historical score backtest from the public nba_data PBP releases.

The model is validation-only. It does not alter current projections or picks.
Each season is evaluated independently to prevent stale cross-offseason form.
"""
import json
import os
import tempfile
from collections import defaultdict, deque
from datetime import datetime, timezone
from urllib.request import Request, urlopen

SOURCE = "https://github.com/ramirobentes/nba_data"
SEASONS = (2025, 2026, 2027)
BASE = "https://github.com/ramirobentes/nba_data/releases/download/pbp/pbp_regular_{season}.parquet"
MARGIN_SD = 12.0


def _db():
    url = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not configured")
    import psycopg
    return psycopg.connect(url, connect_timeout=8)


def _initialize():
    with _db() as conn:
        conn.execute("""
          CREATE TABLE IF NOT EXISTS nba_historical_backtests (
            id BIGSERIAL PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            status TEXT NOT NULL,
            results JSONB NOT NULL
          )
        """)


def status():
    _initialize()
    with _db() as conn:
        row = conn.execute(
            "SELECT created_at,status,results FROM nba_historical_backtests ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return {
        "source": SOURCE,
        "seasons_requested": list(SEASONS),
        "model_influence": "disabled_pending_walk_forward_validation",
        "latest_run": None if not row else {
            "created_at": row[0].isoformat(),
            "status": row[1],
            "results": row[2],
        },
    }


def _download(season):
    url = BASE.format(season=season)
    req = Request(url, headers={"User-Agent": "Colts-NBA-Research/1.0"})
    with urlopen(req, timeout=25) as response:
        data = response.read()
    if len(data) < 1000:
        raise RuntimeError(f"Season {season} Parquet release was unexpectedly small")
    handle = tempfile.NamedTemporaryFile(suffix=f"_{season}.parquet", delete=False)
    try:
        handle.write(data)
        return handle.name
    finally:
        handle.close()


def _games(path):
    import duckdb
    con = duckdb.connect()
    try:
        columns = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{path}')").fetchall()}
        required = {"game_date", "game_id", "team_home", "team_away", "hs", "vs", "season_type"}
        missing = sorted(required - columns)
        if missing:
            raise RuntimeError("PBP schema missing required fields: " + ", ".join(missing))
        sql = f"""
          SELECT game_date, game_id, team_home, team_away,
                 MAX(hs) AS home_score, MAX(vs) AS away_score,
                 MAX(poss_home) AS home_possessions,
                 MAX(poss_away) AS away_possessions
          FROM read_parquet('{path}')
          WHERE LOWER(season_type)='regular'
          GROUP BY game_date, game_id, team_home, team_away
          HAVING MAX(hs)>0 AND MAX(vs)>0
          ORDER BY game_date, game_id
        """
        rows = con.execute(sql).fetchall()
        return rows
    finally:
        con.close()


def _team_rates(history, league_ppp, league_pace):
    # Recent form only; every observation in history is from an earlier date.
    games = list(history)[-10:]
    if len(games) < 3:
        return None
    pf = sum(g["pf"] for g in games)
    pa = sum(g["pa"] for g in games)
    own_poss = sum(g["poss"] for g in games)
    opp_poss = sum(g["opp_poss"] for g in games)
    if own_poss <= 0 or opp_poss <= 0:
        return None
    off = pf / own_poss
    defense = pa / opp_poss
    pace = sum(g["pace"] for g in games) / len(games)
    # Modest regression to the contemporaneous league scoring/pace environment.
    off = 0.85 * off + 0.15 * league_ppp
    defense = 0.85 * defense + 0.15 * league_ppp
    pace = 0.85 * pace + 0.15 * league_pace
    return off, defense, pace



FEATURES=["intercept","home_off_delta","home_def_delta","away_off_delta","away_def_delta","pace_delta","home_rest_delta","away_rest_delta","home_back_to_back","away_back_to_back"]

def _ridge(xtx,xty,penalty=2.0):
    n=len(xty); a=[r[:] for r in xtx]; b=xty[:]
    for i in range(1,n): a[i][i]+=penalty
    a[0][0]+=1e-8
    for c in range(n):
        p=max(range(c,n),key=lambda r:abs(a[r][c]))
        if abs(a[p][c])<1e-10: continue
        a[c],a[p]=a[p],a[c]; b[c],b[p]=b[p],b[c]
        q=a[c][c]
        for j in range(c,n): a[c][j]/=q
        b[c]/=q
        for r in range(n):
            if r==c: continue
            q=a[r][c]
            for j in range(c,n): a[r][j]-=q*a[c][j]
            b[r]-=q*b[c]
    return b

def _rest_days(history,day):
    if not history: return 2.0
    try:
        now=datetime.fromisoformat(str(day)[:10]).date()
        prev=datetime.fromisoformat(str(history[-1]["date"])[:10]).date()
        return max(0.0,min(7.0,float((now-prev).days)))
    except Exception: return 2.0

def _xfeatures(h,a,league_ppp,league_pace,hr,ar):
    hoff,hdef,hpace=h; aoff,adef,apace=a
    return [1.0,(hoff-league_ppp)*100.0,(hdef-league_ppp)*100.0,
      (aoff-league_ppp)*100.0,(adef-league_ppp)*100.0,
      (hpace+apace)/2.0-league_pace,hr-2.0,ar-2.0,
      1.0 if hr<=1 else 0.0,1.0 if ar<=1 else 0.0]

def _resid_sd(values,fallback):
    if len(values)<30: return fallback
    mean=sum(values)/len(values)
    return max(1.0,(sum((v-mean)**2 for v in values)/len(values))**0.5)


def run():
    import math
    files=[]; all_results=[]; season_summaries=[]
    p=len(FEATURES); xtx=[[0.0]*p for _ in range(p)]
    xtym=[0.0]*p; xtyt=[0.0]*p; trained=0; reg_scored=0
    margin_res=[]; total_res=[]
    try:
        for season in SEASONS:
            path=_download(season); files.append(path); rows=_games(path)
            if not rows: raise RuntimeError(f"Season {season}: no regular-season games found")
            histories=defaultdict(lambda:deque(maxlen=10)); league_games=[]; by_date=defaultdict(list); season_predictions=[]
            for row in rows:
                day,gid,home,away,hs,vs,hp,ap=row
                if hp is None or ap is None or float(hp)<=0 or float(ap)<=0: continue
                by_date[str(day)].append((str(gid),str(home),str(away),float(hs),float(vs),float(hp),float(ap)))
            for day in sorted(by_date):
                prior=league_games[-200:]
                if prior:
                    poss=sum(g["poss"]+g["opp_poss"] for g in prior)
                    lppp=sum(g["pf"]+g["pa"] for g in prior)/poss if poss else 1.12
                    lpace=sum(g["pace"] for g in prior)/len(prior)
                else: lppp,lpace=1.12,99.0
                mc=_ridge(xtx,xtym) if trained>=100 else None
                tc=_ridge(xtx,xtyt) if trained>=100 else None
                msd=_resid_sd(margin_res[-500:],14.81); tsd=_resid_sd(total_res[-500:],19.06)
                scale=msd*math.sqrt(3.0)/math.pi
                scored=[]
                for gid,home,away,hs,vs,hp,ap in by_date[day]:
                    h=_team_rates(histories[home],lppp,lpace); a=_team_rates(histories[away],lppp,lpace)
                    if h is None or a is None: continue
                    hr=_rest_days(histories[home],day); ar=_rest_days(histories[away],day)
                    x=_xfeatures(h,a,lppp,lpace,hr,ar)
                    hoff,hdef,hpce=h; aoff,adef,apce=a; pace=(hpce+apce)/2
                    hh=((hoff+adef)/2)*pace+1.0; ah=((aoff+hdef)/2)*pace-1.0
                    hm=hh-ah; ht=hh+ah
                    pm=sum(v*c for v,c in zip(x,mc)) if mc else hm
                    pt=sum(v*c for v,c in zip(x,tc)) if tc else ht
                    am=hs-vs; at=hs+vs; me=pm-am; te=pt-at
                    ph=1/(1+math.exp(-max(-35,min(35,pm/max(.1,scale)))))
                    scored.append({"date":day,"features":x,"margin_error":me,"total_error":te,
                      "heuristic_margin_error":hm-am,"heuristic_total_error":ht-at,
                      "margin_baseline_error":2.0-am,"total_baseline_error":lppp*lpace*2-at,
                      "pred_margin":pm,"actual_margin":am,"pred_total":pt,"actual_total":at,
                      "winner_correct":int((pm>0)==(am>0)),"winner_brier":(ph-(1.0 if am>0 else 0.0))**2,
                      "baseline_brier":(0.5-(1.0 if am>0 else 0.0))**2,
                      "margin_80":int(abs(me)<=1.281551565545*msd),"total_80":int(abs(te)<=1.281551565545*tsd)})
                season_predictions.extend(scored)
                # Same-date outcomes are added only after every game has been forecast.
                for item in scored:
                    x=item["features"]
                    for i in range(p):
                        xtym[i]+=x[i]*item["actual_margin"]; xtyt[i]+=x[i]*item["actual_total"]
                        for j in range(p): xtx[i][j]+=x[i]*x[j]
                    trained+=1
                    margin_res.append(item["margin_error"]); total_res.append(item["total_error"])
                    if mc: reg_scored+=1
                for gid,home,away,hs,vs,hp,ap in by_date[day]:
                    pace=(hp+ap)/2
                    league_games.append({"pf":hs,"pa":vs,"poss":hp,"opp_poss":ap,"pace":pace})
                    histories[home].append({"pf":hs,"pa":vs,"poss":hp,"opp_poss":ap,"pace":pace,"date":day})
                    histories[away].append({"pf":vs,"pa":hs,"poss":ap,"opp_poss":hp,"pace":pace,"date":day})
            if not season_predictions: raise RuntimeError(f"Season {season}: insufficient prior team games")
            n=len(season_predictions)
            season_summaries.append({"season_end_year":season,"games_in_source":len(rows),"games_tested":n,
              "date_start":min(x["date"] for x in season_predictions),"date_end":max(x["date"] for x in season_predictions),
              "margin_mae":round(sum(abs(x["margin_error"]) for x in season_predictions)/n,3),
              "total_mae":round(sum(abs(x["total_error"]) for x in season_predictions)/n,3),
              "heuristic_margin_mae":round(sum(abs(x["heuristic_margin_error"]) for x in season_predictions)/n,3),
              "heuristic_total_mae":round(sum(abs(x["heuristic_total_error"]) for x in season_predictions)/n,3),
              "winner_accuracy":round(sum(x["winner_correct"] for x in season_predictions)/n,4),
              "winner_brier":round(sum(x["winner_brier"] for x in season_predictions)/n,4),
              "margin_80_interval_coverage":round(sum(x["margin_80"] for x in season_predictions)/n,4),
              "total_80_interval_coverage":round(sum(x["total_80"] for x in season_predictions)/n,4)})
            all_results.extend(season_predictions)
        n=len(all_results); me=[x["margin_error"] for x in all_results]; te=[x["total_error"] for x in all_results]
        mb=sum(me)/n; tb=sum(te)/n
        m_sd=(sum((v-mb)**2 for v in me)/n)**.5; t_sd=(sum((v-tb)**2 for v in te)/n)**.5
        mc=_ridge(xtx,xtym); tc=_ridge(xtx,xtyt)
        cov=sum((x-mb)*(y-tb) for x,y in zip(me,te))/n
        results={"source":SOURCE,"method":"Prequential ridge linear regression of offense/defense deviations, pace and rest/back-to-back features; fit only on prior completed dates.",
          "model_type":"ridge_linear_regression","feature_names":FEATURES,"training_games":trained,"regression_games_scored":reg_scored,
          "margin_coefficients":[round(v,8) for v in mc],"total_coefficients":[round(v,8) for v in tc],
          "season_summaries":season_summaries,"games_tested":n,
          "margin_mae":round(sum(abs(v) for v in me)/n,3),"total_mae":round(sum(abs(v) for v in te)/n,3),
          "heuristic_margin_mae":round(sum(abs(x["heuristic_margin_error"]) for x in all_results)/n,3),
          "heuristic_total_mae":round(sum(abs(x["heuristic_total_error"]) for x in all_results)/n,3),
          "baseline_home_advantage_margin_mae":round(sum(abs(x["margin_baseline_error"]) for x in all_results)/n,3),
          "baseline_recent_league_total_mae":round(sum(abs(x["total_baseline_error"]) for x in all_results)/n,3),
          "margin_bias":round(mb,3),"total_bias":round(tb,3),
          "margin_error_rmse":round((sum(v*v for v in me)/n)**.5,3),"total_error_rmse":round((sum(v*v for v in te)/n)**.5,3),
          "margin_error_sd":round(m_sd,3),"total_error_sd":round(t_sd,3),
          "winner_accuracy":round(sum(x["winner_correct"] for x in all_results)/n,4),
          "winner_brier":round(sum(x["winner_brier"] for x in all_results)/n,4),
          "50_50_brier_baseline":round(sum(x["baseline_brier"] for x in all_results)/n,4),
          "margin_80_interval_coverage":round(sum(x["margin_80"] for x in all_results)/n,4),
          "total_80_interval_coverage":round(sum(x["total_80"] for x in all_results)/n,4),
          "score_distribution":{"family":"joint bivariate normal approximation","margin_sd":round(m_sd,3),"total_sd":round(t_sd,3),
            "margin_total_error_covariance":round(cov,3),"calibration":"rolling prequential logistic probabilities and 80% interval coverage"},
          "limitations":["No historical DraftKings closing lines: betting ROI, spread cover calibration and CLV unavailable.",
            "No historical injury, travel or elevation archive; rest/back-to-back are included.",
            "Model remains validation-only until it beats the heuristic and naive baselines across seasons.",
            "Raw source data is not redistributed; repository license status is not established."],
          "model_influence":"disabled_pending_walk_forward_validation","completed_at":datetime.now(timezone.utc).isoformat()}
        _initialize()
        with _db() as conn: conn.execute("INSERT INTO nba_historical_backtests(status,results) VALUES (%s,%s)",("complete",json.dumps(results)))
        return {"success":True,"status":"complete","results":results}
    except Exception as exc:
        result={"error_type":type(exc).__name__,"error":str(exc)[:500],"model_influence":"disabled_pending_walk_forward_validation"}
        try:
            _initialize()
            with _db() as conn: conn.execute("INSERT INTO nba_historical_backtests(status,results) VALUES (%s,%s)",("failed",json.dumps(result)))
        except Exception: pass
        raise
    finally:
        for path in files:
            try: os.unlink(path)
            except OSError: pass
