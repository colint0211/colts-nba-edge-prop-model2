#!/usr/bin/env python3
"""Conservative NBA dataset audit. Never edits source files or silently drops rows."""
import argparse,csv,hashlib,json,math,re,statistics,sys
from collections import defaultdict
from datetime import date,datetime
from pathlib import Path

ALIASES={"game_id":["game_id","gameid","event_id","match_id"],"player_id":["player_id","playerid","athlete_id","person_id"],"player_name":["player_name","player","athlete_name","name"],"game_date":["game_date","date","date_et","game_day","event_date"],"tipoff":["tipoff","tipoff_time","start_time","game_time","scheduled_start"]}
STATS={"points":(["pts","points"],100),"rebounds":(["reb","rebounds","trb"],40),"assists":(["ast","assists"],35),"steals":(["stl","steals"],15),"blocks":(["blk","blocks"],15),"threes_made":(["fg3m","3pm","three_pointers_made","threes_made"],25),"minutes":(["min","minutes","minutes_played"],70)}
DATE_FORMATS=("%Y-%m-%d","%m/%d/%Y","%Y%m%d","%d-%b-%Y","%Y/%m/%d")
TIME_TOKENS=("prediction_timestamp","feature_available_at","source_timestamp","published_at","reported_at","captured_at","updated_at")
POST_TOKENS=("final","actual","boxscore","box_score","postgame","post_game","in_game","live_stat","minutes_played")
def nk(x): return re.sub(r"[^a-z0-9]+","_",str(x or "").strip().lower()).strip("_")
def findcol(headers,names):
 d={nk(h):h for h in headers}
 return next((d[nk(x)] for x in names if nk(x) in d),None)
def pdate(v):
 s=str(v or "").strip()
 try:return datetime.fromisoformat(s.replace("Z","+00:00")).date()
 except ValueError:pass
 for fmt in DATE_FORMATS:
  try:return datetime.strptime(s,fmt).date()
  except ValueError:pass
 return None
def pdt(v):
 try:return datetime.fromisoformat(str(v).strip().replace("Z","+00:00"))
 except (ValueError,TypeError):return None
def pname(v):return re.sub(r"[^a-z0-9]+"," ",str(v or "").lower()).strip()
def add_issue(out,row,severity,code,field,value,message):
 out.append({"row_number":row,"severity":severity,"issue_code":code,"field":field or "","observed_value":str(value)[:300],"message":message,"action":"review"})
def read_rows(path):
 if path.suffix.lower()==".csv":
  with path.open(newline="",encoding="utf-8-sig") as f:
   rd=csv.DictReader(f)
   if not rd.fieldnames:raise ValueError("CSV has no header")
   return list(rd),list(rd.fieldnames)
 if path.suffix.lower() in (".parquet",".pq"):
  try:import pandas as pd
  except ImportError as e:raise RuntimeError("Parquet requires pandas and pyarrow") from e
  df=pd.read_parquet(path)
  return df.astype(object).where(df.notna(),"").to_dict("records"),list(df.columns)
 raise ValueError("Only CSV and Parquet inputs are supported")
def audit(path,outdir,season_start=None,season_end=None):
 rows,headers=read_rows(path);issues=[];corrections=[]
 cols={k:findcol(headers,v) for k,v in ALIASES.items()}
 statcols={k:findcol(headers,v[0]) for k,v in STATS.items()}
 gamecol,playercol,namecol,datecol,tipcol=(cols[k] for k in ("game_id","player_id","player_name","game_date","tipoff"))
 games=defaultdict(list);pg=defaultdict(list);hashes=defaultdict(list);n2i=defaultdict(set);i2n=defaultdict(set);stats=defaultdict(list);clean=[]
 missing_by_col={h:sum(1 for rr in rows if not str(rr.get(h,"") or "").strip()) for h in headers}
 for rn,src in enumerate(rows,2):
  row=dict(src)
  for h in headers:
   if not str(row.get(h,"") or "").strip(): add_issue(issues,rn,"warning","missing_value",h,"","Field is blank or null in this source row.")
  for key,val in list(row.items()):
   if isinstance(val,str) and val!=val.strip():
    row[key]=val.strip();corrections.append({"row_number":rn,"field":key,"before":val,"after":row[key],"reason":"trim_whitespace"})
  for label,cc in (("game_id",gamecol),("game_date",datecol)):
   if not cc or not str(row.get(cc,"")).strip():add_issue(issues,rn,"error","missing_required_field",cc or label,row.get(cc,""),"Required field missing.")
  if playercol and not str(row.get(playercol,"")).strip():add_issue(issues,rn,"warning","missing_player_id",playercol,row.get(playercol,""),"Do not infer identity from name alone.")
  if datecol and row.get(datecol):
   d=pdate(row[datecol])
   if d is None:add_issue(issues,rn,"error","unparseable_date",datecol,row[datecol],"Date cannot be parsed; original retained.")
   else:
    iso=d.isoformat()
    if row[datecol]!=iso:corrections.append({"row_number":rn,"field":datecol,"before":row[datecol],"after":iso,"reason":"normalize_date_iso"});row[datecol]=iso
    if season_start and d<date.fromisoformat(season_start):add_issue(issues,rn,"warning","date_before_season",datecol,iso,"Before configured season start.")
    if season_end and d>date.fromisoformat(season_end):add_issue(issues,rn,"warning","date_after_season",datecol,iso,"After configured season end.")
  tip=pdt(row.get(tipcol,"")) if tipcol else None
  if tipcol and row.get(tipcol) and tip is None:add_issue(issues,rn,"warning","unparseable_tipoff",tipcol,row[tipcol],"Cannot complete temporal leakage checks.")
  if tip and tip.tzinfo is None:add_issue(issues,rn,"error","naive_tipoff_timestamp",tipcol,row[tipcol],"Tipoff timezone offset required.")
  for h in headers:
   key=nk(h);val=str(row.get(h,"") or "").strip()
   if not val:continue
   if any(t in key for t in TIME_TOKENS):
    stamp=pdt(val)
    if stamp is None:add_issue(issues,rn,"warning","unparseable_source_timestamp",h,val,"Cannot verify point-in-time availability.")
    elif stamp.tzinfo is None:add_issue(issues,rn,"error","naive_source_timestamp",h,val,"Source timestamp timezone offset required.")
    elif tip and tip.tzinfo and stamp>tip:add_issue(issues,rn,"error","source_after_tipoff",h,val,"Value captured/published after tipoff; forbidden as pregame feature.")
   if any(t in key for t in POST_TOKENS):add_issue(issues,rn,"warning","possible_post_tipoff_field",h,val,"Field name suggests actual/live/post-game data; use only as outcome, not predictor.")
  pid=str(row.get(playercol,"")).strip() if playercol else "";nm=pname(row.get(namecol,"")) if namecol else ""
  if pid and nm:n2i[nm].add(pid);i2n[pid].add(nm)
  for stat,(aliases,maximum) in STATS.items():
   cc=statcols[stat]
   if not cc or not str(row.get(cc,"")).strip():continue
   try:x=float(str(row[cc]).replace(",",""))
   except ValueError:add_issue(issues,rn,"error","invalid_numeric_stat",cc,row[cc],f"{stat} must be numeric.");continue
   if not math.isfinite(x):add_issue(issues,rn,"error","invalid_numeric_stat",cc,row[cc],f"{stat} must be finite.");continue
   stats[stat].append((rn,x))
   if x<0 or x>maximum:add_issue(issues,rn,"error","implausible_stat_range",cc,x,f"{stat} outside broad range 0..{maximum}; verify source.")
   if stat!="minutes" and not x.is_integer():add_issue(issues,rn,"warning","non_integer_count_stat",cc,x,f"{stat} is normally an integer.")
  if gamecol and row.get(gamecol):
   gid=str(row[gamecol]).strip();games[gid].append(rn)
   if playercol and pid:pg[(gid,pid)].append(rn)
  hashes[hashlib.sha256(json.dumps(row,sort_keys=True,default=str).encode()).hexdigest()].append(rn);clean.append(row)
 for gid,nums in games.items():
  if len(nums)>1 and not playercol:
   for rn in nums:add_issue(issues,rn,"warning","duplicate_game_id",gamecol,gid,f"Repeated game ID on rows {nums}; inspect.")
 for key,nums in pg.items():
  if len(nums)>1:
   for rn in nums:add_issue(issues,rn,"error","duplicate_player_game",f"{gamecol}+{playercol}",key,f"Duplicate player/game key on rows {nums}; retained.")
 for _,nums in hashes.items():
  if len(nums)>1:
   for rn in nums:add_issue(issues,rn,"warning","exact_duplicate_row","row",nums,f"Exact duplicate rows {nums}; retained.")
 for nm,ids in n2i.items():
  if len(ids)>1:add_issue(issues,0,"warning","name_maps_to_multiple_player_ids",namecol,nm,f"Name maps to IDs {sorted(ids)}; review.")
 for pid,names in i2n.items():
  if len(names)>1:add_issue(issues,0,"warning","player_id_maps_to_multiple_names",playercol,pid,f"ID maps to names {sorted(names)}; review.")
 for stat,pairs in stats.items():
  vals=[v for _,v in pairs]
  if len(vals)>=8:
   med=statistics.median(vals);mad=statistics.median(abs(v-med) for v in vals)
   if mad>0:
    for rn,v in pairs:
     rz=.6745*(v-med)/mad
     if abs(rz)>=6:add_issue(issues,rn,"warning","robust_stat_outlier",statcols[stat],v,f"{stat} robust z={rz:.2f}; flagged, not altered.")
 rowcodes=defaultdict(set)
 for it in issues:
  if it["row_number"]>0:rowcodes[it["row_number"]].add(it["issue_code"])
 flag="audit_issue_codes"
 while flag in headers:flag="_"+flag
 for rn,row in enumerate(clean,2):row[flag]=";".join(sorted(rowcodes[rn]))
 outdir.mkdir(parents=True,exist_ok=True);stem=path.stem
 paths={"cleaned_csv":outdir/f"{stem}.cleaned.csv","issues_csv":outdir/f"{stem}.issues.csv","corrections_csv":outdir/f"{stem}.corrections.csv","audit_json":outdir/f"{stem}.audit.json"}
 with paths["cleaned_csv"].open("w",newline="",encoding="utf-8") as f:
  w=csv.DictWriter(f,fieldnames=headers+[flag],extrasaction="ignore");w.writeheader();w.writerows(clean)
 for key,records,fields in (("issues_csv",issues,["row_number","severity","issue_code","field","observed_value","message","action"]),("corrections_csv",corrections,["row_number","field","before","after","reason"])):
  with paths[key].open("w",newline="",encoding="utf-8") as f:
   w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(records)
 summary={"input_file":str(path),"rows_read":len(rows),"rows_written":len(clean),"rows_deleted":0,"columns":headers,"missing_values_by_column":missing_by_col,"detected_columns":cols,"detected_stat_columns":statcols,"correction_count":len(corrections),"issue_count":len(issues),"unresolved_issues":len(issues),"issue_counts_by_code":{k:sum(z["issue_code"]==k for z in issues) for k in sorted({z["issue_code"] for z in issues})},"player_name_to_multiple_ids":{k:sorted(v) for k,v in n2i.items() if len(v)>1},"player_id_to_multiple_names":{k:sorted(v) for k,v in i2n.items() if len(v)>1},"duplicate_game_ids":{k:v for k,v in games.items() if len(v)>1},"duplicate_player_game_keys":{f"{k[0]}::{k[1]}":v for k,v in pg.items() if len(v)>1},"outputs":{k:str(v) for k,v in paths.items()},"note":"No rows deleted. Only whitespace and parseable date formatting normalized. All anomalies remain for explicit review."}
 paths["audit_json"].write_text(json.dumps(summary,indent=2),encoding="utf-8");return summary
def main():
 ap=argparse.ArgumentParser(description="Audit NBA CSV/Parquet files without deleting records.")
 ap.add_argument("input");ap.add_argument("--out",default="backtest-output/data_audit");ap.add_argument("--season-start");ap.add_argument("--season-end");a=ap.parse_args()
 p=Path(a.input);files=sorted(p.rglob("*")) if p.is_dir() else [p];files=[x for x in files if x.is_file() and x.suffix.lower() in (".csv",".parquet",".pq")]
 if not files:raise SystemExit("No CSV/Parquet files found.")
 results=[]
 for x in files:
  try:results.append(audit(x,Path(a.out),a.season_start,a.season_end))
  except Exception as e:results.append({"input_file":str(x),"audit_failed":type(e).__name__,"detail":str(e)});print(f"AUDIT FAILED {x}: {e}",file=sys.stderr)
 overall={"files_audited":len(results),"files":results,"total_rows":sum(x.get("rows_read",0) for x in results),"total_corrections":sum(x.get("correction_count",0) for x in results),"total_unresolved_issues":sum(x.get("unresolved_issues",0) for x in results),"rows_deleted":0}
 Path(a.out).mkdir(parents=True,exist_ok=True);(Path(a.out)/"NBA_DATA_AUDIT_SUMMARY.json").write_text(json.dumps(overall,indent=2),encoding="utf-8");print(json.dumps(overall,indent=2))
if __name__=="__main__":main()
