import os
import json
import math
import random
from difflib import SequenceMatcher
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from fastapi import FastAPI, Request as FastRequest, HTTPException
from fastapi.responses import HTMLResponse, Response, JSONResponse
from . import prediction_ledger
HTML = '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#07111f"><title>Colt\'s NBA Edge</title><style>\n:root{color-scheme:dark;--bg:#07111f;--panel:#101d30;--panel2:#14243a;--line:#263a53;--text:#eff6ff;--muted:#9aacc3;--green:#47e0a1;--red:#ff7e8a;--gold:#ffd27a}*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at top,#132b46 0%,var(--bg) 55%);color:var(--text);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}header{padding:24px clamp(14px,4vw,42px);background:rgba(8,18,32,.86);border-bottom:1px solid var(--line)}main{max-width:1440px;margin:auto;padding:22px clamp(12px,3vw,30px) 44px}.headrow{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap}h1{font-size:clamp(23px,4vw,34px);letter-spacing:-.7px;margin:0;font-weight:850}.brand{color:var(--green)}p,.muted,small{color:var(--muted)}.bar,.controls,.tabs{display:flex;gap:9px;align-items:center;flex-wrap:wrap;margin:16px 0}.pill,button,input,select{padding:10px 12px;background:var(--panel2);color:inherit;border:1px solid var(--line);border-radius:10px}button{cursor:pointer;font-weight:750}button:hover{border-color:var(--green)}button.active{background:#123c32;border-color:#2d9876;color:var(--green)}.statuspill{border-radius:999px;padding:7px 11px;background:#102b2a;border:1px solid #215b50;color:var(--green);font-size:12px;font-weight:800}.statrow{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:11px}.stat{padding:15px;background:linear-gradient(145deg,#14243a,#0e1a2c);border:1px solid var(--line);border-radius:15px}.statlabel{color:var(--muted);font-size:12px}.num{font-size:27px;font-weight:850;margin-top:3px}.sectiontitle{font-size:19px;font-weight:850;margin:25px 0 5px}.sectionhint{color:var(--muted);font-size:12px;margin-bottom:12px}.games{display:flex;flex-wrap:wrap;gap:7px}.game{padding:7px 10px;border:1px solid var(--line);border-radius:9px;background:#0f1d30;color:#dbe8f9;font-size:12px}.game.live{border-color:#2c8d6b;background:#102e2a}.controls input{flex:1;min-width:180px}.cards{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:13px}.player-card{min-width:0;padding:16px;background:linear-gradient(145deg,#14253b,#0c1828);border:1px solid var(--line);border-radius:16px;box-shadow:0 10px 30px #0002;position:relative;overflow:hidden}.player-card:before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:#38506d}.player-card.value:before{background:var(--green)}.player-top{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}.player-name{font-size:17px;font-weight:850;line-height:1.2}.matchup{font-size:12px;color:var(--muted);margin-top:5px}.decision{font-size:10px;font-weight:850;letter-spacing:.4px;border-radius:999px;padding:5px 8px;background:#27354a;color:#dce7f5;white-space:nowrap}.decision.value{background:#103c30;color:var(--green)}.decision.lean{background:#3b3020;color:var(--gold)}.decision.pass{background:#3b2029;color:var(--red)}.scorebox{display:flex;justify-content:space-between;align-items:center;gap:10px;margin:15px 0 12px;padding:11px 12px;background:#091625;border:1px solid #20354d;border-radius:12px}.score-label{font-size:10px;text-transform:uppercase;letter-spacing:1px;color:var(--muted);font-weight:850}.score{font-size:16px;font-weight:900;white-space:nowrap}.game-status{font-size:11px;color:var(--muted);text-align:right}.live-dot{display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--green);margin-right:5px;vertical-align:middle}.statline{display:grid;grid-template-columns:1fr 1fr;gap:10px}.mini{background:#15263c;border:1px solid #293e58;border-radius:12px;padding:11px}.mini-label{font-size:11px;color:var(--muted)}.mini-value{font-size:23px;font-weight:900;margin-top:2px}.mini-value span{font-size:12px;color:var(--muted);font-weight:700}.progress{height:5px;border-radius:99px;background:#263950;overflow:hidden;margin-top:8px}.progress>span{display:block;height:100%;background:linear-gradient(90deg,#31c98c,#8af0c5);border-radius:99px;width:0}.card-bottom{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;border-top:1px solid #243850;margin-top:13px;padding-top:11px;font-size:12px;color:var(--muted)}.grade{font-weight:900}.grade.hit{color:var(--green)}.grade.miss{color:var(--red)}.empty{padding:28px;border:1px dashed var(--line);border-radius:14px;text-align:center;color:var(--muted);grid-column:1/-1}.ev{font-size:17px;font-weight:900;color:var(--green)}.footer{font-size:12px;margin-top:22px;color:var(--muted)}@media(max-width:1050px){.cards{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:650px){header{padding:18px 14px}main{padding:16px 11px 32px}.statrow{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.stat{padding:12px}.num{font-size:23px}.cards{grid-template-columns:1fr;gap:10px}.player-card{padding:14px}.sectiontitle{margin-top:20px}.tabs button{flex:1;min-width:28%}}\n.reasoning{margin-top:12px;border:1px solid #2a4058;border-radius:11px;background:#0b1727;padding:10px 12px}.reasoning summary{cursor:pointer;font-weight:800;color:#cde3ff}.reasoning-body{padding-top:9px;color:var(--muted);font-size:12px}.reasoning-body div{margin:5px 0}.reasoning-body b{color:var(--text)}.reason-warning{color:var(--gold)!important}</style></head><body><header><div class="headrow"><div><h1><span class="brand">COLT\'S</span> NBA EDGE</h1><p style="margin:6px 0 0">NBA player prop value board · live game scores · live player points</p></div><span class="statuspill" id="status">LOADING SLATE</span></div><div class="bar"><button onclick="load(true)">↻ Refresh board</button><label style="display:flex;gap:7px;align-items:center;color:var(--muted);font-size:12px">Bankroll ($)<input id="bankroll" type="number" min="0" step="1" placeholder="Enter bankroll" style="width:145px" oninput="render();renderFull()"></label><span id="updated" class="muted">Loading today\'s games…</span><span id="liveUpdated" class="muted"></span></div></header><main>\n<div class="statrow"><div class="stat"><div class="statlabel">Today\'s games</div><div id="ng" class="num">—</div></div><div class="stat"><div class="statlabel">Prop lines loaded</div><div id="nl" class="num">—</div></div><div class="stat"><div class="statlabel">Projections calculated</div><div id="np" class="num">—</div></div><div class="stat"><div class="statlabel">Positive EV candidates</div><div id="nv" class="num">—</div></div></div>\n<div class="sectiontitle">Today\'s scoreboard</div><div id="games" class="games"><span class="muted">Loading games…</span></div>\n<div class="sectiontitle" id="topTitle">Top 5 Points</div><div class="sectionhint" id="topHint">Top five points projections ranked by expected value when available.</div>\n<div class="tabs" id="tabs"><button data-market="ev" onclick="setMarket(\'ev\')">Top 5 EV</button><button class="active" data-market="points" onclick="setMarket(\'points\')">Top 5 Points</button><button data-market="rebounds" onclick="setMarket(\'rebounds\')">Top 5 Rebounds</button><button data-market="steals" onclick="setMarket(\'steals\')">Top 5 Steals</button><button data-market="assists" onclick="setMarket(\'assists\')">Top 5 Assists</button><button data-market="threes" onclick="setMarket(\'threes\')">Top 5 3-Pointers Made</button></div>\n<div class="controls"><input class="pill" id="search" placeholder="Search player or matchup" oninput="render()"><span id="count" class="muted"></span></div><div id="cards" class="cards"><div class="empty">Calculating today\'s best plays…</div></div>\n<div class="sectiontitle">Full player prop board</div><div class="sectionhint" id="fullHint">All currently sourced points props, including leans and passes.</div><div class="controls"><select class="pill" id="filter" onchange="renderFull()"><option value="all">All picks</option><option value="value">Value candidates</option><option value="lean">Leans / verify</option><option value="pass">Pass</option></select><input class="pill" id="searchFull" placeholder="Search full board" oninput="renderFull()"><span id="countFull" class="muted"></span></div><div id="fullCards" class="cards"></div>\n<div class="sectiontitle">Model tracking</div><div class="sectionhint">Saved outcomes only. Performance is not validated until enough results settle.</div><div class="statrow"><div class="stat"><div class="statlabel">Recorded results</div><div id="btPredictions" class="num">—</div></div><div class="stat"><div class="statlabel">Wins / losses</div><div id="btSettled" class="num">—</div></div><div class="stat"><div class="statlabel">Win rate</div><div id="btWinRate" class="num">—</div></div><div class="stat"><div class="statlabel">Net profit</div><div id="btRoi" class="num">—</div></div></div><div class="bar"><button onclick="loadBacktest()">Refresh tracking</button><span id="btUpdated" class="muted"></span></div><div id="btStatus" class="muted">Loading saved results…</div><div class="footer">Live scores and player points are supplied by ESPN public endpoints. BettingPros supplies the Rebounds, Assists, Steals, and 3-Pointers Made markets. EV plays are shown only when the API response includes the required projection and expected-value fields; lines alone are not treated as picks. Live data refreshes every 15 seconds while this page is open.</div></main>\n<script>\nlet data=[],liveData={games:[],players_by_id:{},players_by_name:{}},market=\'points\',bpCache={},bpLoading={};\nconst el=id=>document.getElementById(id);\nconst esc=s=>String(s??\'\').replace(/[&<>"\']/g,c=>({\'&\':\'&amp;\',\'<\':\'&lt;\',\'>\':\'&gt;\',\'"\':\'&quot;\',"\'":\'&#39;\'}[c]));\nconst num=(n,d=1)=>n==null||!Number.isFinite(Number(n))?\'—\':Number(n).toFixed(d);\nconst norm=s=>String(s||\'\').toLowerCase().replace(/[^a-z0-9]/g,\'\');\nconst bpMarkets=[\'points\',\'rebounds\',\'assists\',\'steals\',\'threes\'];\nfunction bpMarketLabel(m){return ({points:\'Points\',rebounds:\'Rebounds\',assists:\'Assists\',steals:\'Steals\',threes:\'3-Pointers Made\'})[m]||m}\nfunction gameFor(p){\n  const raw=String(p.game||\'\').toUpperCase();\n  const teams=raw.split(\'@\').map(x=>x.trim()).sort().join(\'|\');\n  return (liveData.games||[]).find(g=>(g.teams||[]).slice().sort().join(\'|\')===teams);\n}\nfunction playerLive(p){\n  const id=p.athlete_id==null?\'\':String(p.athlete_id);\n  return (id&&liveData.players_by_id?.[id])||liveData.players_by_name?.[norm(p.player)]||null;\n}\nfunction grade(p){\n  const g=gameFor(p),st=playerLive(p);\n  if(!g||g.state!==\'post\'||!st||st.points==null||p.line==null||!p.best_side)return null;\n  const side=String(p.best_side).toUpperCase(),line=Number(p.line),pts=Number(st.points);\n  if(pts===line)return \'PUSH\';\n  return (side===\'OVER\'?pts>line:pts<line)?\'HIT\':\'MISS\';\n}\nfunction isFallback(p){\n  const label=String(p.sample_label||p.reason||\'\').toLowerCase();\n  return label.includes(\'previous-season\')||label.includes(\'previous season\')||label.includes(\'fallback\');\n}\nfunction rankEV(a){\n  return a.filter(p=>p.model_status===\'projected\'&&!isFallback(p)&&p.expected_profit_per_100!=null&&Number(p.expected_profit_per_100)>0&&p.best_side)\n    .slice().sort((x,y)=>Number(y.expected_profit_per_100)-Number(x.expected_profit_per_100)\n      ||Number(y.edge_pp||-999)-Number(x.edge_pp||-999)\n      ||String(x.player||\'\').localeCompare(String(y.player||\'\'))\n      ||String(x.game||\'\').localeCompare(String(y.game||\'\')));\n}\nfunction setMarket(m){\n  market=m;\n  document.querySelectorAll(\'#tabs button\').forEach(b=>b.classList.toggle(\'active\',b.dataset.market===m));\n  const labels={ev:\'Top 5 EV Plays\',points:\'Top 5 Points\',rebounds:\'Top 5 Rebounds\',assists:\'Top 5 Assists\',steals:\'Top 5 Steals\',threes:\'Top 5 3-Pointers Made\'};\n  el(\'topTitle\').textContent=labels[m]||\'Top 5 Plays\';\n  el(\'topHint\').textContent=m===\'points\'?\'Top five proprietary points projections.\':m===\'ev\'?\'Top five positive-EV plays ranked across points, rebounds, assists, steals and three-pointers made.\':\'Proprietary \'+bpMarketLabel(m)+\' projections, probabilities and EV. BettingPros supplies lines and odds only.\';\n  el(\'fullHint\').textContent=m===\'ev\'?\'All positive-EV candidates across all five markets.\':m===\'points\'?\'All proprietary points props.\':\'All proprietary \'+bpMarketLabel(m)+\' projections using BettingPros lines and odds only.\';\n  render();renderFull();\n  if(m===\'ev\')[\'rebounds\',\'assists\',\'steals\',\'threes\'].forEach(loadBP);\n  else if([\'rebounds\',\'assists\',\'steals\',\'threes\'].includes(m))loadBP(m);\n}\nasync function load(refresh=false){\n  el(\'status\').textContent=\'CALCULATING\';\n  try{\n    const r=await fetch(\'/api/auto-board\'+(refresh?\'?refresh=true\':\'\'),{cache:\'no-store\'}),d=await r.json();\n    if(!r.ok)throw Error(d.detail?.message||d.detail||\'Board unavailable\');\n    data=d.props||[];\n    el(\'ng\').textContent=d.schedule_game_count??\'—\';\n    el(\'nl\').textContent=d.line_count??data.length;\n    el(\'np\').textContent=d.projected_count??0;\n    el(\'nv\').textContent=data.filter(x=>x.recommendation===\'VALUE CANDIDATE\'&&!isFallback(x)&&Number(x.expected_profit_per_100)>0).length;\n    el(\'updated\').textContent=\'Board updated \'+new Date(d.calculated_at||Date.now()).toLocaleTimeString();\n    el(\'status\').textContent=d.status===\'ready\'?\'BOARD READY\':\'DATA LIMITED\';\n    await updateLive();render();renderFull();\n  }catch(e){\n    el(\'status\').textContent=\'BOARD UNAVAILABLE\';\n    el(\'cards\').innerHTML=\'<div class="empty">\'+esc(e.message)+\'</div>\';\n  }\n}\nfunction renderGames(){\n  const a=liveData.games||[];\n  if(!a.length){el(\'games\').innerHTML=\'<span class="muted">No games found for today.</span>\';return}\n  el(\'games\').innerHTML=a.map(g=>{\n    const s=g.state===\'in\'?\'live\':g.state===\'post\'?\'final\':\'\';\n    const score=g.away_score!=null&&g.home_score!=null?g.away+\' \'+g.away_score+\' – \'+g.home_score+\' \'+g.home:g.away+\' @ \'+g.home;\n    return \'<div class="game \'+s+\'"><b>\'+esc(score)+\'</b><span> · \'+esc(g.status||\'Scheduled\')+\'</span></div>\';\n  }).join(\'\');\n}\nasync function updateLive(){\n  try{\n    const r=await fetch(\'/api/live-stats?ts=\'+Date.now(),{cache:\'no-store\'});\n    if(!r.ok)throw Error(\'live endpoint\');\n    liveData=await r.json();renderGames();\n    el(\'liveUpdated\').textContent=\'Scores checked \'+new Date(liveData.updated_at||Date.now()).toLocaleTimeString();\n  }catch(_){el(\'liveUpdated\').textContent=\'Live feed temporarily unavailable\'}\n}\nfunction quarterValues(p){\n  const st=playerLive(p)||{};\n  const candidates=[st,p];\n  const pick=(keys)=>{\n    for(const obj of candidates){\n      for(const key of keys){\n        const v=obj?.[key];\n        if(v!==undefined&&v!==null&&Number.isFinite(Number(v)))return Number(v);\n      }\n    }\n    return null;\n  };\n  const arr=st.points_by_quarter||st.quarter_points||st.period_points||p.points_by_quarter||p.quarter_points||p.period_points;\n  const vals=Array.isArray(arr)?arr:[];\n  const obj=st.quarters||p.quarters||{};\n  return [1,2,3,4].map((q,i)=>{\n    const v=vals[i]??obj[\'q\'+q]??obj[\'Q\'+q]??obj[q]??pick([\'q\'+q+\'_points\',\'Q\'+q+\'_points\',\'period\'+q+\'_points\',\'quarter\'+q+\'_points\']);\n    return v==null?\'—\':num(v,0);\n  });\n}\nfunction reasoning(p){\n  const gap=Number(p.projection)-Number(p.line),rows=[];\n  rows.push(\'<div><b>Projection vs line:</b> \'+(Number.isFinite(gap)?(gap>=0?\'+\':\'\')+num(gap,2)+\' points \'+(gap>=0?\'above\':\'below\')+\' the line\':\'Unavailable\')+\'.</div>\');\n  rows.push(\'<div><b>Recent scoring:</b> \'+(p.recent5_avg==null?\'Not supplied\':num(p.recent5_avg,1)+\' PPG\')+\'.</div>\');\n  rows.push(\'<div><b>Sample:</b> \'+(p.games_used??\'—\')+\' games used (\'+esc(p.sample_label||\'sample period unavailable\')+\').</div>\');\n  rows.push(\'<div><b>Reliability:</b> \'+esc(p.confidence||\'not supplied\')+\' confidence. Probabilities are not calibrated until outcomes are tested.</div>\');\n  if(isFallback(p))rows.push(\'<div class="reason-warning"><b>Fallback safeguard:</b> This projection uses previous-season data. It is shown for context only and is excluded from Top 5 EV.</div>\');\n  if(p.reason)rows.push(\'<div><b>Model note:</b> \'+esc(p.reason)+\'.</div>\');\n  return \'<details class="reasoning"><summary>Why this projection? Data &amp; confidence</summary><div class="reasoning-body">\'+rows.join(\'\')+\'</div></details>\';\n}\nfunction card(p,i){\n  const g=gameFor(p),st=playerLive(p),gr=grade(p),pts=st&&st.points!=null?Number(st.points):null;\n  const line=Number(p.line),progress=pts==null||!Number.isFinite(line)||line<=0?0:Math.max(0,Math.min(100,pts/line*100));\n  const fallback=isFallback(p);\n  const shownRecommendation=fallback?\'PASS · FALLBACK\':(p.recommendation||p.model_status||\'—\');\n  const d=fallback?\'pass\':p.recommendation===\'VALUE CANDIDATE\'?\'value\':p.recommendation===\'LEAN / VERIFY\'?\'lean\':\'pass\';\n  const score=g?(g.away_score!=null&&g.home_score!=null?g.away+\' \'+g.away_score+\' – \'+g.home_score+\' \'+g.home:g.away+\' @ \'+g.home):\'Score unavailable\';\n  const liveText=g&&g.state===\'in\'?\'<span class="live-dot"></span>\'+esc((g.period?\'Q\'+g.period:\'LIVE\')+(g.clock&&g.clock!==\'0:00\'?\' · \'+g.clock+\' left\':\'\')):esc(g?g.status:\'Game status unavailable\');\n  const gradeHtml=gr?\'<span class="grade \'+(gr===\'HIT\'?\'hit\':gr===\'MISS\'?\'miss\':\'push\')+\'">\'+(gr===\'HIT\'?\'✓ HIT\':gr===\'MISS\'?\'✕ MISS\':\'PUSH\')+\'</span>\':\'<span>Pending</span>\';\n  const quarters=quarterValues(p);\n  return \'<article class="player-card \'+d+\'"><div class="player-top"><div><div class="player-name">\'+esc((i+1)+\'. \'+p.player+(p.roster_team?\' (\'+p.roster_team+\')\':\'\'))+\'</div><div class="matchup">\'+esc(p.game||\'Matchup unavailable\')+\' · \'+esc(p.sample_label||\'Projection unavailable\')+\'</div></div><span class="decision \'+d+\'">\'+esc(shownRecommendation)+\'</span></div>\'+\n  \'<div class="scorebox"><div><div class="score-label">Live game score</div><div class="score">\'+esc(score)+\'</div></div><div class="game-status">\'+liveText+\'</div></div>\'+\n  \'<div class="statline"><div class="mini"><div class="mini-label">Live points / prop line</div><div class="mini-value">\'+(pts==null?\'—\':pts)+\' <span>/ \'+num(p.line)+\' pts</span></div><div class="progress"><span style="width:\'+progress+\'%"></span></div></div><div class="mini"><div class="mini-label">Model projection</div><div class="mini-value">\'+num(p.projection)+\' <span>pts</span></div><div class="mini-label">\'+num(p.best_probability)+\'% best-side probability</div></div></div>\'+\n  \'<div class="mini" style="margin-top:10px"><div class="mini-label">Points by quarter (only when supplied by live feed)</div><div class="statline">\'+quarters.map((v,i)=>\'<div><div class="mini-label">Q\'+(i+1)+\'</div><div class="mini-value" style="font-size:18px">\'+esc(v)+\'</div></div>\').join(\'\')+\'</div></div>\'+\n  \'<div class="card-bottom"><span>Pick: <b>\'+esc(p.best_side||\'—\')+\'</b> · \'+num(p.edge_pp)+\' pp edge</span><span class="ev">\'+(p.expected_profit_per_100==null?\'—\':\'$\'+num(p.expected_profit_per_100,2)+\' EV\')+\'</span></div>\'+\n  \'<div class="card-bottom"><span>Odds O \'+esc(p.over_odds)+\' / U \'+esc(p.under_odds)+\'</span><span>\'+gradeHtml+\'</span></div>\'+\n  \'<div class="card-bottom"><button onclick="recordResult(this)" data-player="\'+esc(p.player)+\'" data-game="\'+esc(p.game)+\'" data-line="\'+esc(p.line)+\'" data-side="\'+esc(p.best_side)+\'" data-odds="\'+esc(p.best_odds)+\'" data-prob="\'+esc(p.best_probability)+\'" \'+(p.model_status===\'projected\'&&p.best_side?\'\':\'disabled\')+\'>Record final points</button></div>\'+reasoning(p)+\'</article>\';\n}\nfunction bpNorm(p,m){\n  const pr=p.projection||{},participant=p.participant||{},playerObj=participant.player||{},event=p.event||{},over=p.over||{},under=p.under||{};\n  const evFrom=(prob,price)=>{\n    const q=Number(prob),o=Number(price);\n    if(prob==null||price==null||!Number.isFinite(q)||!Number.isFinite(o)||q<0||q>1||o===0)return null;\n    const payout=o<0?100/Math.abs(o):o/100;\n    return q*payout-(1-q);\n  };\n  let side=String(pr.recommended_side||\'\').toLowerCase();\n  if(side!==\'over\'&&side!==\'under\'){\n    const oe=over.expected_value!=null?Number(over.expected_value):evFrom(over.probability,over.odds);\n    const ue=under.expected_value!=null?Number(under.expected_value):evFrom(under.probability,under.odds);\n    if(oe!=null||ue!=null)side=ue==null||(oe!=null&&oe>=ue)?\'over\':\'under\';\n  }\n  const selected=side===\'under\'?under:over;\n  const probability=pr.probability!=null?Number(pr.probability):(selected.probability!=null?Number(selected.probability):null);\n  const ev=pr.expected_value!=null?Number(pr.expected_value):(selected.expected_value!=null?Number(selected.expected_value):evFrom(probability,selected.odds));\n  let player=participant.name||p.player_name||(typeof p.player===\'string\'?p.player:null)||playerObj.name||\'Unknown player\';\n  if(typeof playerObj===\'string\')player=participant.name||p.player_name||playerObj;\n  const line=over.line!=null?over.line:(under.line!=null?under.line:(p.line??p.line_value??null));\n  return {raw:p,market:m,player,team:playerObj.team||participant.team_abbreviation||participant.team||p.team_abbreviation||\'\',game:event.name||p.event_name||p.matchup||\'\',line,overOdds:over.odds??p.over_odds??null,underOdds:under.odds??p.under_odds??null,projection:pr.value??p.projection_value??null,side:side||null,probability,ev:ev==null?null:ev,rating:pr.bet_rating??selected.bet_rating??null};\n}\nasync function loadBP(m){\n  if(bpCache[m]||bpLoading[m])return;\n  bpLoading[m]=true;\n  if(market===m)el(\'cards\').innerHTML=\'<div class="empty">Calculating proprietary \'+esc(bpMarketLabel(m))+\' model…</div>\';\n  try{\n    const r=await fetch(\'/api/proprietary-market-board?market=\'+encodeURIComponent(m)+\'&ts=\'+Date.now(),{cache:\'no-store\'});\n    const d=await r.json();\n    if(!r.ok)throw Error(d.detail||d.error||\'Proprietary model request failed\');\n    bpCache[m]=(d.props||[]).map(p=>({...p,overOdds:p.over_odds,underOdds:p.under_odds,ev:p.expected_value_pct==null?null:Number(p.expected_value_pct)/100,expected_profit_per_100:p.expected_value_pct,model_status:\'projected\',best_side:p.best_side,best_probability:p.best_probability,market:m,simulations:p.simulations})).filter(p=>p.player&&p.line!=null);\n    bpLoading[m]=false;render();renderFull();\n  }catch(e){bpLoading[m]=false;bpCache[m]={error:e.message};render();renderFull()}\n}\nfunction bpLineCard(p,i){\n  const odds=x=>x==null?\'—\':(Number(x)>0?\'+\':\'\')+x;\n  const bankroll=Number(el(\'bankroll\')?.value||0);\n  const qualifies=p.recommendation===\'VALUE CANDIDATE\'&&Number(p.expected_value_pct)>5;\n  const stake=bankroll>0&&qualifies?bankroll*0.05:null;\n  const cls=p.recommendation===\'VALUE CANDIDATE\'?\'value\':p.recommendation===\'LEAN / VERIFY\'?\'lean\':\'pass\';\n  const label=bpMarketLabel(p.market);\n  return \'<article class="player-card \'+cls+\'"><div class="player-top"><div><div class="player-name">\'+esc((i+1)+\'. \'+p.player+(p.team?\' (\'+p.team+\')\':\'\'))+\'</div><div class="matchup">\'+esc(p.game||\'NBA player prop\')+\' · Proprietary ESPN game-log model</div></div><span class="decision \'+cls+\'">\'+esc(p.recommendation||\'PASS\')+\'</span></div>\'+\n    \'<div class="scorebox"><div><div class="score-label">\'+esc(label)+\' line</div><div class="score">\'+esc(p.line)+\'</div></div><div class="game-status">Over \'+esc(odds(p.overOdds??p.over_odds))+\'<br>Under \'+esc(odds(p.underOdds??p.under_odds))+\'</div></div>\'+\n    \'<div class="statline"><div class="mini"><div class="mini-label">Our projection</div><div class="mini-value">\'+num(p.projection,2)+\'</div><div class="mini-label">\'+esc(p.sample_label||\'Game-log sample\')+\'</div></div><div class="mini"><div class="mini-label">Over / Under / Push</div><div class="mini-value">\'+num(p.over_probability,1)+\'% / \'+num(p.under_probability,1)+\'%</div><div class="mini-label">Push \'+num(p.push_probability,1)+\'%</div></div></div>\'+\n    \'<div class="card-bottom"><span>Model pick: <b>\'+esc(p.best_side||\'—\')+\'</b> · \'+num(p.edge_pp)+\' pp edge</span><span class="ev">\'+(p.expected_value_pct==null?\'—\':(Number(p.expected_value_pct)>0?\'+\':\'\')+num(p.expected_value_pct,2)+\'% EV\')+\'</span></div>\'+\n    \'<div class="card-bottom"><span>Simulations: \'+esc(p.simulations||50000).toLocaleString()+\'</span><span>Odds O \'+esc(odds(p.overOdds??p.over_odds))+\' / U \'+esc(odds(p.underOdds??p.under_odds))+\'</span></div>\'+\n    \'<div class="card-bottom"><span>DK/FD market: \'+esc((p.market_books||[]).join(\', \')||\'No verified pair\')+\'</span><span>Stake: \'+(stake==null?\'Enter bankroll / no qualifying bet\':\'$\'+num(stake,2)+\' (5%)\')+\'</span></div>\'+\n    \'<div class="card-bottom"><span>Kelly diagnostic: \'+(p.kelly_fraction_pct==null?\'—\':num(p.kelly_fraction_pct,2)+\'%\')+\'</span><span>50/50 model-market blend</span></div></article>\';\n}\nfunction allModelEV(){\n  const all=rankEV(data).map(p=>({...p,market:\'points\'}));\n  for(const m of [\'rebounds\',\'assists\',\'steals\',\'threes\']){\n    const rows=bpCache[m];\n    if(Array.isArray(rows))all.push(...rows.filter(p=>p.model_status===\'projected\'&&p.expected_value_pct!=null&&Number(p.expected_value_pct)>0&&p.best_side&&!isFallback(p)));\n  }\n  return all.filter(p=>p.model_status===\'projected\'&&!isFallback(p)&&p.expected_profit_per_100!=null&&Number(p.expected_profit_per_100)>0&&p.best_side)\n    .sort((a,b)=>Number(b.expected_profit_per_100)-Number(a.expected_profit_per_100)||Number(b.edge_pp||-999)-Number(a.edge_pp||-999)||String(a.player).localeCompare(String(b.player)));\n}\nfunction bpCard(p,i){\n  const hasProjection=p.projection!=null&&Number.isFinite(Number(p.projection));\n  const hasEV=p.ev!=null&&Number.isFinite(Number(p.ev));\n  const positive=hasEV&&Number(p.ev)>0&&p.side;\n  const decision=positive?\'POSITIVE EV\':hasProjection?\'PROJECTION ONLY\':\'LINE ONLY\';\n  const cls=positive?\'value\':hasProjection?\'lean\':\'pass\';\n  const odds=x=>x==null?\'—\':(Number(x)>0?\'+\':\'\')+x;\n  const evText=hasEV?((Number(p.ev)>0?\'+\':\'\')+num(Number(p.ev)*100,1)+\'%\'):\'—\';\n  return \'<article class="player-card \'+cls+\'"><div class="player-top"><div><div class="player-name">\'+esc((i+1)+\'. \'+p.player+(p.team?\' (\'+p.team+\')\':\'\'))+\'</div><div class="matchup">\'+esc(p.game||\'NBA player prop\')+\' · BettingPros</div></div><span class="decision \'+cls+\'">\'+decision+\'</span></div>\'+\n    \'<div class="scorebox"><div><div class="score-label">\'+esc(bpMarketLabel(p.market))+\' line</div><div class="score">\'+esc(p.line)+\'</div></div><div class="game-status">Over \'+esc(odds(p.overOdds))+\'<br>Under \'+esc(odds(p.underOdds))+\'</div></div>\'+\n    \'<div class="statline"><div class="mini"><div class="mini-label">BettingPros projection</div><div class="mini-value">\'+(hasProjection?num(p.projection,2):\'—\')+\'</div><div class="mini-label">\'+(p.side?\'Suggested side: \'+esc(String(p.side).toUpperCase()):\'No verified side supplied\')+\'</div></div><div class="mini"><div class="mini-label">Expected value</div><div class="mini-value">\'+evText+\'</div><div class="mini-label">\'+(p.probability!=null&&Number.isFinite(Number(p.probability))?\'Projection probability \'+num(Number(p.probability)>1?Number(p.probability):Number(p.probability)*100,1)+\'%\':(p.rating!=null?\'Bet rating \'+esc(p.rating):\'Not supplied by API response\'))+\'</div></div></div>\'+\n    \'<div class="card-bottom"><span>Market: <b>\'+esc(bpMarketLabel(p.market))+\'</b></span><span>Source: BettingPros API</span></div></article>\';\n}\nfunction render(){\n  const q=el(\'search\').value.toLowerCase();let list=[];\n  if(market===\'ev\'){\n    list=allModelEV().slice(0,5);\n    if(q)list=list.filter(p=>(p.player+\' \'+(p.team||p.roster_team||\'\')+\' \'+(p.game||\'\')).toLowerCase().includes(q));\n    el(\'count\').textContent=list.length+\' positive-EV plays across all five markets\';\n    if(!list.length){el(\'cards\').innerHTML=\'<div class="empty">No verified positive-EV plays across the five model markets right now.</div>\';return}\n    el(\'cards\').innerHTML=list.map((p,i)=>p.market===\'points\'?card(p,i):bpLineCard(p,i)).join(\'\');\n    return;\n  }\n  if(market===\'points\'){\n    list=rankEV(data).slice(0,5);\n    if(q)list=list.filter(p=>(p.player+\' \'+p.game).toLowerCase().includes(q));\n    el(\'count\').textContent=list.length+\' model plays\';\n    el(\'cards\').innerHTML=list.map((p,i)=>card(p,i)).join(\'\')||\'<div class="empty">No verified points projections available.</div>\';\n    return;\n  }\n  const a=bpCache[market];\n  if(a&&a.error){el(\'count\').textContent=\'Model unavailable\';el(\'cards\').innerHTML=\'<div class="empty">\'+esc(a.error)+\'</div>\';return}\n  if(!Array.isArray(a)){el(\'count\').textContent=\'Loading\';el(\'cards\').innerHTML=\'<div class="empty">Loading proprietary \'+esc(bpMarketLabel(market))+\' model…</div>\';return}\n  list=a.slice().sort((x,y)=>Number(y.expected_profit_per_100??-999)-Number(x.expected_profit_per_100??-999));\n  if(q)list=list.filter(p=>(p.player+\' \'+p.team+\' \'+p.game).toLowerCase().includes(q));\n  list=list.slice(0,5);\n  el(\'count\').textContent=list.length+\' model projections\';\n  el(\'cards\').innerHTML=list.map((p,i)=>bpLineCard(p,i)).join(\'\')||\'<div class="empty">No eligible \'+esc(bpMarketLabel(market))+\' projections. Players without sufficient verified game logs are excluded.</div>\';\n}\nfunction renderFull(){\n  const z=el(\'filter\').value,q=el(\'searchFull\').value.toLowerCase();\n  if(market===\'points\'){\n    const rows=data.filter(p=>(z===\'all\'||z===\'value\'&&p.recommendation===\'VALUE CANDIDATE\'||z===\'lean\'&&p.recommendation===\'LEAN / VERIFY\'||z===\'pass\'&&p.recommendation===\'PASS\')&&(p.player+\' \'+p.game).toLowerCase().includes(q));\n    el(\'countFull\').textContent=rows.length+\' / \'+data.length+\' points props\';\n    el(\'fullCards\').innerHTML=rows.map((p,i)=>card(p,i)).join(\'\')||\'<div class="empty">No points props match this filter.</div>\';\n    return;\n  }\n  if(market===\'ev\'){\n    const rows=allModelEV().filter(p=>(p.player+\' \'+(p.team||\'\')+\' \'+(p.game||\'\')).toLowerCase().includes(q));\n    el(\'countFull\').textContent=rows.length+\' cross-market EV candidates\';\n    el(\'fullCards\').innerHTML=rows.map((p,i)=>p.market===\'points\'?card(p,i):bpLineCard(p,i)).join(\'\')||\'<div class="empty">No positive-EV plays available.</div>\';\n    return;\n  }\n  const a=bpCache[market];\n  if(a&&a.error){el(\'countFull\').textContent=\'Model unavailable\';el(\'fullCards\').innerHTML=\'<div class="empty">\'+esc(a.error)+\'</div>\';return}\n  if(!Array.isArray(a)){el(\'countFull\').textContent=\'Loading\';el(\'fullCards\').innerHTML=\'<div class="empty">Loading proprietary \'+esc(bpMarketLabel(market))+\' model…</div>\';return}\n  const rows=a.filter(p=>(p.player+\' \'+p.team+\' \'+p.game).toLowerCase().includes(q));\n  el(\'countFull\').textContent=rows.length+\' / \'+a.length+\' model projections\';\n  el(\'fullCards\').innerHTML=rows.map((p,i)=>bpLineCard(p,i)).join(\'\')||\'<div class="empty">No verified \'+esc(bpMarketLabel(market))+\' projections available.</div>\';\n}\nasync function loadBacktest(){\n  try{\n    const r=await fetch(\'/api/tracking?ts=\'+Date.now(),{cache:\'no-store\'}),d=await r.json();\n    if(!r.ok)throw Error(d.detail?.message||d.detail||\'Tracking unavailable\');\n    el(\'btPredictions\').textContent=d.count??0;el(\'btSettled\').textContent=(d.wins??0)+\' / \'+(d.losses??0);\n    el(\'btWinRate\').textContent=d.win_rate_pct==null?\'—\':num(d.win_rate_pct,1)+\'%\';\n    el(\'btRoi\').textContent=\'$\'+num(d.net_profit_units,2);el(\'btUpdated\').textContent=\'Updated \'+new Date().toLocaleTimeString();\n    el(\'btStatus\').textContent=(d.pushes??0)+\' pushes. Net assumes $100 risk per recorded result; this is tracking, not a historical backtest.\';\n  }catch(e){el(\'btStatus\').textContent=\'Tracking unavailable: \'+String(e.message||e)}\n}\nasync function recordResult(btn){\n  const actual=prompt(\'Enter FINAL POINTS for \'+btn.dataset.player+\' (\'+btn.dataset.game+\').\');\n  if(actual===null||actual.trim()===\'\')return;\n  const pts=Number(actual);if(!Number.isFinite(pts)||pts<0){alert(\'Enter a valid non-negative points total.\');return}\n  try{\n    const r=await fetch(\'/api/track-result\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({player:btn.dataset.player,game:btn.dataset.game,line:Number(btn.dataset.line),side:btn.dataset.side,odds:Number(btn.dataset.odds),model_probability_pct:Number(btn.dataset.prob),actual_points:pts})});\n    const d=await r.json();if(!r.ok)throw Error(d.detail?.message||d.detail||\'Could not record result\');\n    alert(\'Saved result: \'+d.record.result+\'. Net \'+num(d.record.profit,2)+\' per $100 risked.\');await loadBacktest();\n  }catch(e){alert(\'Could not save result: \'+String(e.message||e))}\n}\nload();loadBacktest();\nsetInterval(async()=>{await updateLive();render();renderFull()},15000);\nsetInterval(()=>load(false),300000);\n</script></body></html>'
UPSTREAM = "https://colts-nba-edge-prop-model-e2oyr9l03-colin-82ad.vercel.app"
LIVE_UPSTREAM = "https://colts-nba-edge-prop-model-3xj6ghyhl-colin-82ad.vercel.app"
app = FastAPI()
BP_MARKETS = {"points": 156, "rebounds": 157, "assists": 151, "steals": 160, "threes": 162}
@app.get("/", response_class=HTMLResponse)
async def home():
    return HTMLResponse(HTML)
ESPN_BASE = "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba"
ESPN_SEARCH = "https://site.web.api.espn.com/apis/search/v2"
STAT_FIELDS = {
    "rebounds": ("rebounds", "REB"),
    "assists": ("assists", "AST"),
    "steals": ("steals", "STL"),
    "threes": ("threePointsMade", "3PT"),
}
_ESPN_ID_CACHE = {}
_ESPN_LOG_CACHE = {}

def _get_json(url, timeout=12):
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 ColtsNBAEdge/1.0", "Accept": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except HTTPError as exc:
        detail = exc.read(500).decode("utf-8", "replace").replace("\\n", " ")
        raise RuntimeError("ESPN HTTP " + str(exc.code) + ": " + detail[:220]) from exc

def _norm_player_name(value):
    return "".join(ch.lower() for ch in str(value or "") if ch.isalnum())

def _espn_player_id(name):
    """Resolve a player to ESPN's numeric NBA athlete ID.

    ESPN global search can return UUID-style content IDs. The common-v3
    gamelog endpoint requires the numeric athlete ID used in ESPN profile
    URLs / athlete UIDs, so never pass an arbitrary search-result UUID.
    """
    key = _norm_player_name(name)
    if key in _ESPN_ID_CACHE:
        return _ESPN_ID_CACHE[key]

    query = urlencode({"query": name, "limit": 20, "sport": "basketball", "league": "nba"})
    payload = _get_json(ESPN_SEARCH + "?" + query)
    candidates = []

    def numeric_id(value):
        if value is None:
            return None
        text = str(value).strip()
        return text if text.isdigit() else None

    def id_from_uid(value):
        text = str(value or "")
        # ESPN athlete UIDs commonly contain an a:<numeric athlete id> segment.
        for marker in ("~a:", "a:"):
            pos = text.find(marker)
            if pos >= 0:
                tail = text[pos + len(marker):]
                token = tail.split("~", 1)[0].split(":", 1)[0].split("/", 1)[0]
                found = numeric_id(token)
                if found:
                    return found
        return None

    def id_from_href(value):
        text = str(value or "")
        # ESPN profile URLs commonly contain /id/<numeric-id>/.
        marker = "/id/"
        pos = text.find(marker)
        if pos >= 0:
            tail = text[pos + len(marker):]
            token = tail.split("/", 1)[0].split("?", 1)[0]
            found = numeric_id(token)
            if found:
                return found
        return None

    def walk(value):
        if isinstance(value, dict):
            athlete_obj = value.get("athlete") if isinstance(value.get("athlete"), dict) else value
            candidate_name = (
                athlete_obj.get("fullName") or athlete_obj.get("displayName")
                or athlete_obj.get("name") or value.get("title") or ""
            )
            candidate_id = (
                numeric_id(athlete_obj.get("id"))
                or id_from_uid(athlete_obj.get("uid"))
                or id_from_href(athlete_obj.get("href"))
                or id_from_href(value.get("href"))
                or id_from_href(value.get("link"))
            )
            links = athlete_obj.get("links") or value.get("links") or []
            if not candidate_id and isinstance(links, list):
                for link in links:
                    if isinstance(link, dict):
                        candidate_id = id_from_href(link.get("href"))
                        if candidate_id:
                            break
            if candidate_id and candidate_name:
                candidates.append((candidate_id, str(candidate_name)))
            for child in value.values():
                if isinstance(child, (dict, list)):
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    best_id, best_score = None, 0
    for candidate_id, candidate_name in candidates:
        score = SequenceMatcher(None, key, _norm_player_name(candidate_name)).ratio()
        if score > best_score:
            best_id, best_score = candidate_id, score

    if best_score < 0.78:
        best_id = None

    _ESPN_ID_CACHE[key] = best_id
    return best_id

def _gamelog_values(athlete_id, field, label, season):
    """Read per-game stat rows from ESPN's nested common-v3 gamelog schema."""
    cache_key = (str(athlete_id), field, season)
    if cache_key in _ESPN_LOG_CACHE:
        return _ESPN_LOG_CACHE[cache_key]
    url = ESPN_BASE + "/athletes/" + str(athlete_id) + "/gamelog?" + urlencode({"season": season})
    payload = _get_json(url)
    if not isinstance(payload, dict):
        _ESPN_LOG_CACHE[cache_key] = []
        return []
    labels = payload.get("labels") or []
    names = payload.get("names") or payload.get("statNames") or []
    # Labels match the stats arrays; names may also include DATE/OPP/RESULT.
    index = labels.index(label) if isinstance(labels, list) and label in labels else -1
    rows = []
    season_types = payload.get("seasonTypes") or []
    if isinstance(season_types, list):
        for season_type in season_types:
            if not isinstance(season_type, dict):
                continue
            for category in season_type.get("categories") or []:
                if not isinstance(category, dict):
                    continue
                cat_labels = category.get("labels") or labels
                cat_names = category.get("names") or names
                cat_index = cat_labels.index(label) if isinstance(cat_labels, list) and label in cat_labels else -1
                if cat_index < 0 and isinstance(cat_names, list) and field in cat_names:
                    cat_index = cat_names.index(field)
                cat_rows = category.get("events") or []
                if isinstance(cat_rows, dict):
                    cat_rows = list(cat_rows.values())
                if isinstance(cat_rows, list):
                    rows.extend((row, cat_index) for row in cat_rows if isinstance(row, dict))
    if not rows:
        direct = payload.get("gameLog") or payload.get("games") or payload.get("events") or []
        if isinstance(direct, dict):
            direct = list(direct.values())
        if isinstance(direct, list):
            rows = [(row, index) for row in direct if isinstance(row, dict) and isinstance(row.get("stats"), list)]
    values = []
    for row, row_index in rows:
        stats = row.get("stats") or []
        if not isinstance(stats, list):
            continue
        use_index = row_index if row_index >= 0 else index
        if use_index < 0 and isinstance(names, list) and len(names) == len(stats) and field in names:
            use_index = names.index(field)
        if use_index < 0 or use_index >= len(stats):
            continue
        raw_value = str(stats[use_index]).replace(",", "").strip()
        if field == "threePointsMade" and "-" in raw_value:
            raw_value = raw_value.split("-", 1)[0].strip()
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value >= 0:
            values.append(value)
    _ESPN_LOG_CACHE[cache_key] = values
    return values

def _player_history(athlete_id, field, label):
    current = _gamelog_values(athlete_id, field, label, 2027)
    previous = _gamelog_values(athlete_id, field, label, 2026) if len(current) < 5 else []
    values = current + previous
    return values[-25:], ("2026-27 + 2025-26" if previous else "2026-27 regular season")

def _odds_profit(win_probability, loss_probability, odds):
    if odds is None:
        return None
    try:
        price = float(odds)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(price) or price == 0:
        return None
    payout = 100.0 / abs(price) if price < 0 else price / 100.0
    return win_probability * payout - loss_probability

def _simulate_market(values, line, over_odds, under_odds, seed, minutes=None, market=""):
    """
    Benter-inspired player-prop distribution model.
    Uses recency-weighted empirical outcomes, shrinkage toward the longer-run
    baseline, minutes normalization, robust dispersion, and deterministic
    Monte Carlo. It is not a trained Benter model: calibration weights remain
    provisional until settled historical predictions are available.
    """
    clean = [float(v) for v in values if v is not None and math.isfinite(float(v)) and float(v) >= 0]
    if len(clean) < 5:
        return None
    recent = clean[-5:]
    long_mean = sum(clean) / len(clean)
    recent_mean = sum(recent) / len(recent)
    # Empirical-Bayes shrinkage: small/recent samples are pulled toward the
    # player's longer-run level rather than being allowed to dominate.
    n = len(clean)
    prior_games = 8.0
    shrunk_mean = (n * long_mean + prior_games * (sum(clean) / n)) / (n + prior_games)
    recency_weight = min(0.35, len(recent) / (len(recent) + 12.0))
    target_mean = (1.0 - recency_weight) * shrunk_mean + recency_weight * recent_mean

    valid_minutes = []
    for item in (minutes or []):
        try:
            value = float(item)
            if math.isfinite(value) and value > 0:
                valid_minutes.append(value)
        except (TypeError, ValueError):
            continue
    projected_min = _projected_minutes(valid_minutes)
    baseline_min = sum(valid_minutes) / len(valid_minutes) if valid_minutes else None
    minute_factor = 1.0
    if projected_min and baseline_min and baseline_min > 0:
        minute_factor = max(0.65, min(1.25, projected_min / baseline_min))
        target_mean *= minute_factor

    # Rescale each empirical game outcome around the shrunk target mean. This
    # preserves observed skew/zero-inflation while avoiding raw recent-form chasing.
    raw_mean = long_mean if long_mean > 0 else 1.0
    distribution = [max(0.0, v * (target_mean / raw_mean)) for v in clean]
    # Winsorize only extreme observations for the center estimate; retain the
    # untrimmed empirical distribution for outcome variance.
    ordered = sorted(distribution)
    median = ordered[len(ordered)//2]
    deviations = sorted(abs(x - median) for x in ordered)
    mad = deviations[len(deviations)//2] if deviations else 0.0
    robust_scale = max(0.0, 1.4826 * mad)
    variance = sum((x - target_mean) ** 2 for x in distribution) / max(1, len(distribution)-1)
    std_dev = max(math.sqrt(max(0.0, variance)), robust_scale, 0.35)

    rng = random.Random(str(seed) + "|" + str(line) + "|" + str(market))
    sims = 60000
    over = under = push = total = 0
    # A mild normal residual component smooths sparse discrete game-log
    # distributions without replacing their empirical shape.
    for _ in range(sims):
        sample = distribution[rng.randrange(len(distribution))]
        if rng.random() < 0.20:
            sample = max(0.0, 0.75 * sample + 0.25 * rng.gauss(target_mean, std_dev))
        total += sample
        if abs(sample - line) < 1e-9:
            push += 1
        elif sample > line:
            over += 1
        else:
            under += 1
    over_p, under_p, push_p = over/sims, under/sims, push/sims
    over_ev = _odds_profit(over_p, under_p, over_odds)
    under_ev = _odds_profit(under_p, over_p, under_odds)
    best = max([(v, side, prob, price) for v, side, prob, price in [
        (over_ev, "OVER", over_p, over_odds), (under_ev, "UNDER", under_p, under_odds)
    ] if v is not None], key=lambda x: x[0], default=None)
    return {
        "projection": round(target_mean, 2),
        "projected_minutes": round(projected_min, 1) if projected_min else None,
        "over_probability": round(over_p * 100, 2),
        "under_probability": round(under_p * 100, 2),
        "push_probability": round(push_p * 100, 2),
        "simulations": sims,
        "best_side": best[1] if best else None,
        "best_probability": round(best[2] * 100, 2) if best else None,
        "best_odds": best[3] if best else None,
        "expected_value_pct": round(best[0] * 100, 2) if best else None,
        "edge_pp": None,
        "recommendation": "VALUE CANDIDATE" if best and best[0] > 0.05 else ("LEAN / VERIFY" if best and best[0] > 0 else "PASS"),
        "model_components": ["empirical outcome distribution", "recency weighting", "long-run shrinkage",
                             "minutes adjustment", "robust dispersion", "Monte Carlo smoothing"],
        "model_method": "Benter-inspired empirical-Bayes distribution; provisional, requires out-of-sample calibration",
        "sample_size": n,
        "long_run_mean": round(long_mean, 2),
        "recent5_mean": round(recent_mean, 2),
        "shrunk_mean": round(target_mean, 2),
        "distribution_std_dev": round(std_dev, 2),
        "minute_adjustment_factor": round(minute_factor, 3),
        "calibration_status": "uncalibrated",
    }

_BP_OFFERS_CACHE = {}
_BP_BOOKS_CACHE = {"at": 0, "ids": {}}

def _american_probability(odds):
    try:
        value = float(odds)
        if not math.isfinite(value) or value == 0:
            return None
        return abs(value) / (abs(value) + 100.0) if value < 0 else 100.0 / (value + 100.0)
    except (TypeError, ValueError):
        return None

def _american_payout(odds):
    value = float(odds)
    return 100.0 / abs(value) if value < 0 else value / 100.0

def _bp_books():
    now = __import__("time").time()
    if now - _BP_BOOKS_CACHE["at"] < 3600 and _BP_BOOKS_CACHE["ids"]:
        return _BP_BOOKS_CACHE["ids"]
    api_key = os.getenv("BETTINGPROS_API_KEY", "").strip()
    if not api_key:
        return {}
    req = Request("https://api.bettingpros.com/v3/books?sport=NBA",
        headers={"x-api-key": api_key, "accept": "application/json", "user-agent": "ColtsNBAEdge/1.0"})
    try:
        with urlopen(req, timeout=12) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
        rows = payload.get("books", []) if isinstance(payload, dict) else []
        ids = {}
        for row in rows:
            name = str(row.get("name") or row.get("title") or "").lower()
            if "draftkings" in name or "draft kings" in name:
                ids["draftkings"] = int(row["id"])
            elif "fanduel" in name or "fan duel" in name:
                ids["fanduel"] = int(row["id"])
        _BP_BOOKS_CACHE.update({"at": now, "ids": ids})
        return ids
    except Exception:
        return {}

def _bp_event_offers(event_id, market):
    key = (str(event_id), market)
    now = __import__("time").time()
    cached = _BP_OFFERS_CACHE.get(key)
    if cached and now - cached[0] < 120:
        return cached[1]
    api_key = os.getenv("BETTINGPROS_API_KEY", "").strip()
    if not api_key:
        return []
    params = urlencode({"sport": "NBA", "market_id": BP_MARKETS[market], "event_id": event_id, "page": 1, "limit": 50})
    req = Request("https://api.bettingpros.com/v3/offers?" + params,
        headers={"x-api-key": api_key, "accept": "application/json", "user-agent": "ColtsNBAEdge/1.0"})
    try:
        with urlopen(req, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
        rows = payload.get("offers", []) if isinstance(payload, dict) else []
        _BP_OFFERS_CACHE[key] = (now, rows)
        return rows
    except Exception:
        return []

def _bp_two_book_market(offers, player_name):
    """Return a same-line DK/FD no-vig blend and the best price at that exact line."""
    books = _bp_books()
    wanted = {books.get("draftkings"), books.get("fanduel")} - {None}
    if not wanted:
        return None
    target = _norm_player_name(player_name)
    matches = []
    for offer in offers:
        participants = offer.get("participants") or []
        names = [_norm_player_name(p.get("name") or "") for p in participants if isinstance(p, dict)]
        if not names or max((SequenceMatcher(None, target, n).ratio() for n in names), default=0) < 0.90:
            continue
        side_prices = {"over": {}, "under": {}}
        for selection in offer.get("selections") or []:
            side = str(selection.get("selection") or "").lower()
            if side not in side_prices:
                continue
            for book in selection.get("books") or []:
                try:
                    book_id = int(book.get("id"))
                except (TypeError, ValueError):
                    continue
                if book_id not in wanted:
                    continue
                for line_obj in book.get("lines") or []:
                    if not line_obj.get("active", True) or line_obj.get("is_off"):
                        continue
                    try:
                        line = float(line_obj.get("line"))
                        odds = float(line_obj.get("cost"))
                    except (TypeError, ValueError):
                        continue
                    if odds == 0:
                        continue
                    side_prices[side].setdefault(line, {})[book_id] = {
                        "odds": odds, "link": line_obj.get("link"), "updated": line_obj.get("updated")
                    }
        common = set(side_prices["over"]) & set(side_prices["under"])
        candidates = []
        for line in common:
            covered = [bid for bid in wanted if bid in side_prices["over"][line] and bid in side_prices["under"][line]]
            if covered:
                candidates.append((len(covered), line, covered))
        if candidates:
            _, line, covered = max(candidates, key=lambda x: (x[0], -x[1]))
            pairs = []
            for bid in covered:
                over = side_prices["over"][line][bid]
                under = side_prices["under"][line][bid]
                qo, qu = _american_probability(over["odds"]), _american_probability(under["odds"])
                if qo is None or qu is None or qo + qu <= 0:
                    continue
                pairs.append({"book_id": bid, "over_probability": qo/(qo+qu),
                              "under_probability": qu/(qo+qu), "over": over, "under": under})
            if pairs:
                matches.append((len(pairs), line, pairs))
    if not matches:
        return None
    _, line, pairs = max(matches, key=lambda x: (x[0], len(x[2])))
    over_prob = sum(x["over_probability"] for x in pairs) / len(pairs)
    under_prob = sum(x["under_probability"] for x in pairs) / len(pairs)
    over_best = max((x["over"] for x in pairs), key=lambda x: _american_payout(x["odds"]))
    under_best = max((x["under"] for x in pairs), key=lambda x: _american_payout(x["odds"]))
    names = {v: k for k, v in books.items()}
    return {"line": line, "over_probability": over_prob, "under_probability": under_prob,
            "over_odds": over_best["odds"], "under_odds": under_best["odds"],
            "over_book": names.get(next(x["book_id"] for x in pairs if x["over"] is over_best), "sportsbook"),
            "under_book": names.get(next(x["book_id"] for x in pairs if x["under"] is under_best), "sportsbook"),
            "books_used": [names.get(x["book_id"], str(x["book_id"])) for x in pairs]}

def _log_pool(model_p, market_p, weight=0.5):
    eps = 1e-6
    a = max(eps, min(1-eps, float(model_p)))
    b = max(eps, min(1-eps, float(market_p)))
    z = math.exp((1-weight)*math.log(a) + weight*math.log(b))
    other = math.exp((1-weight)*math.log(1-a) + weight*math.log(1-b))
    return z / (z + other)

def _score_blended_market(model, market_quote):
    """
    Logarithmic opinion pool on the conditional Over/Under probabilities.
    Push probability remains a separate model outcome, avoiding the previous
    distortion from independently pooling two non-complementary probabilities.
    """
    model_over = model["over_probability"] / 100.0
    model_under = model["under_probability"] / 100.0
    model_push = max(0.0, min(0.25, model.get("push_probability", 0.0) / 100.0))
    model_decision_mass = max(1e-9, model_over + model_under)
    model_over_cond = model_over / model_decision_mass
    market_over = market_quote["over_probability"]
    market_under = market_quote["under_probability"]
    market_mass = max(1e-9, market_over + market_under)
    market_over_cond = market_over / market_mass
    # Fixed 50% market weight is a transparent starting point, not a learned
    # optimum. The response exposes this so calibration can replace it later.
    market_weight = 0.50
    blended_over_cond = _log_pool(model_over_cond, market_over_cond, market_weight)
    blended_under_cond = 1.0 - blended_over_cond
    over = (1.0 - model_push) * blended_over_cond
    under = (1.0 - model_push) * blended_under_cond
    choices = []
    for side, prob, loss, odds in (
        ("OVER", over, under, market_quote["over_odds"]),
        ("UNDER", under, over, market_quote["under_odds"]),
    ):
        ev = _odds_profit(prob, loss, odds)
        if ev is not None:
            choices.append((ev, side, prob, odds, loss))
    best = max(choices, key=lambda x: x[0], default=None)
    model.update({
        "model_over_probability": round(model_over * 100, 2),
        "model_under_probability": round(model_under * 100, 2),
        "market_over_probability": round(market_over * 100, 2),
        "market_under_probability": round(market_under * 100, 2),
        "over_probability": round(over * 100, 2),
        "under_probability": round(under * 100, 2),
        "push_probability": round(model_push * 100, 2),
        "best_side": best[1] if best else None,
        "best_probability": round(best[2] * 100, 2) if best else None,
        "best_odds": best[3] if best else None,
        "expected_value_pct": round(best[0] * 100, 2) if best else None,
        "edge_pp": round((best[2] - _american_probability(best[3])) * 100, 2) if best else None,
        "recommendation": "VALUE CANDIDATE" if best and best[0] > 0.05 else ("LEAN / VERIFY" if best and best[0] > 0 else "PASS"),
        "kelly_fraction_pct": round(max(0.0, (best[2] * _american_payout(best[3]) - best[4]) /
            max(1e-9, _american_payout(best[3]) * (best[2] + best[4]))) * 100, 2) if best else None,
        "blend_method": "50/50 conditional logarithmic pool; market weight awaits out-of-sample optimization",
        "market_weight": market_weight,
        "market_books": market_quote["books_used"],
        "best_over_book": market_quote["over_book"],
        "best_under_book": market_quote["under_book"],
        "calibration_status": "uncalibrated",
    })
    return model

@app.get("/api/proprietary-market-board")
def proprietary_market_board(market: str = "rebounds"):
    market = market.lower().strip()
    if market not in STAT_FIELDS:
        raise HTTPException(status_code=400, detail="Use rebounds, assists, steals, or threes.")
    api_key = os.getenv("BETTINGPROS_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="BettingPros lines unavailable: API key missing.")
    market_id = BP_MARKETS[market]
    bp_url = "https://api.bettingpros.com/v3/props?" + urlencode({"sport": "NBA", "market_id": market_id, "page": 1, "limit": 50})
    try:
        req = Request(bp_url, headers={"x-api-key": api_key, "accept": "application/json", "user-agent": "ColtsNBAEdge/1.0"})
        with urlopen(req, timeout=20) as response:
            bp_payload = json.loads(response.read().decode("utf-8", "replace"))
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Could not retrieve " + market + " prop lines from BettingPros.") from exc
    field, label = STAT_FIELDS[market]
    output, failures = [], []
    props = bp_payload.get("props") or [] if isinstance(bp_payload, dict) else []
    offers_by_event = {}
    for raw in props:
        participant = raw.get("participant") or {}
        player_obj = participant.get("player") or {}
        event = raw.get("event") or {}
        event_id = event.get("id") or raw.get("event_id")
        if event_id and str(event_id) not in offers_by_event:
            offers_by_event[str(event_id)] = _bp_event_offers(event_id, market)
        over, under = raw.get("over") or {}, raw.get("under") or {}
        name = participant.get("name") or raw.get("player_name") or (raw.get("player") if isinstance(raw.get("player"), str) else None) or player_obj.get("name") or player_obj.get("full_name")
        if not name:
            continue
        quote = _bp_two_book_market(offers_by_event.get(str(event_id), []), name) if event_id else None
        line = quote["line"] if quote else over.get("line", under.get("line", raw.get("line", raw.get("line_value"))))
        try:
            line = float(line)
        except (TypeError, ValueError):
            continue
        over_odds = quote["over_odds"] if quote else over.get("odds", raw.get("over_odds"))
        under_odds = quote["under_odds"] if quote else under.get("odds", raw.get("under_odds"))
        try:
            athlete_id = _espn_player_id(name)
            if not athlete_id:
                failures.append(name + ": ESPN player match unavailable")
                continue
            values, sample_label = _player_history(athlete_id, field, label)
            model = _simulate_market(values, line, None, None, _norm_player_name(name) + market)
            if not model:
                failures.append(name + ": fewer than five valid game logs")
                continue
            if quote:
                model = _score_blended_market(model, quote)
            else:
                model["recommendation"] = "PASS"
                model["expected_value_pct"] = None
                model["best_side"] = None
                model["best_probability"] = None
                model["best_odds"] = None
                model["edge_pp"] = None
                model["blend_method"] = "No verified DK/FD two-sided quote at a matching line; no bet rating"
            output.append({
                "player": name,
                "team": player_obj.get("team") or participant.get("team_abbreviation") or participant.get("team") or "",
                "game": event.get("name") or raw.get("event_name") or raw.get("matchup") or "",
                "market": market, "line": line, "over_odds": over_odds, "under_odds": under_odds,
                "sample_label": sample_label, "athlete_id": athlete_id, "games_used": len(values),
                **model,
            })
        except Exception as exc:
            failures.append(name + ": " + type(exc).__name__ + ": " + str(exc)[:180])
    output.sort(key=lambda x: (x.get("expected_value_pct") is None, -(x.get("expected_value_pct") or -999)))
    try:
        prediction_ledger.initialize()
        prediction_ledger.save_predictions(output, market)
    except Exception:
        pass
    return JSONResponse({
        "market": market,
        "source": "ESPN game-log distribution + BettingPros DraftKings/FanDuel no-vig market blend",
        "blend": "50/50 logarithmic opinion pool (provisional pending historical calibration)",
        "line_count": len(props), "projected_count": len(output), "props": output,
        "unavailable": failures[:20],
    })


@app.get("/api/bettingpros-books")
def bettingpros_books():
    """Return active BettingPros sportsbook IDs/names so book IDs are never guessed."""
    api_key = os.getenv("BETTINGPROS_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="BETTINGPROS_API_KEY is missing.")
    req = Request(
        "https://api.bettingpros.com/v3/books?sport=NBA",
        headers={"x-api-key": api_key, "accept": "application/json", "user-agent": "ColtsNBAEdge/1.0"},
        method="GET",
    )
    try:
        with urlopen(req, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except HTTPError as exc:
        raise HTTPException(status_code=502, detail="BettingPros books endpoint returned HTTP " + str(exc.code)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Could not retrieve BettingPros sportsbook IDs.") from exc
    books = payload.get("books", []) if isinstance(payload, dict) else []
    return JSONResponse({"books": books, "count": len(books), "source": "BettingPros API"})


@app.get("/api/bettingpros-offers")
def bettingpros_offers(market: str = "rebounds", event_id: int | None = None):
    """Expose current offers for supported NBA prop markets, preserving per-book prices."""
    market = market.lower().strip()
    market_id = BP_MARKETS.get(market)
    if market_id is None:
        raise HTTPException(status_code=400, detail="Unsupported NBA prop market.")
    api_key = os.getenv("BETTINGPROS_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="BETTINGPROS_API_KEY is missing.")
    params = {"sport": "NBA", "market_id": market_id, "page": 1, "limit": 50}
    if event_id is not None:
        params["event_id"] = event_id
    req = Request(
        "https://api.bettingpros.com/v3/offers?" + urlencode(params),
        headers={"x-api-key": api_key, "accept": "application/json", "user-agent": "ColtsNBAEdge/1.0"},
        method="GET",
    )
    try:
        with urlopen(req, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except HTTPError as exc:
        raise HTTPException(status_code=502, detail="BettingPros offers endpoint returned HTTP " + str(exc.code)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Could not retrieve BettingPros offers.") from exc
    rows = payload.get("offers", []) if isinstance(payload, dict) else []
    return JSONResponse({"market": market, "market_id": market_id, "offers": rows,
                         "pagination": payload.get("_pagination", {}) if isinstance(payload, dict) else {},
                         "source": "BettingPros API"})


@app.get("/api/bettingpros-props")
def bettingpros_props(market: str = "rebounds"):
    market = market.lower().strip()
    market_id = BP_MARKETS.get(market)
    if market_id is None:
        raise HTTPException(status_code=400, detail="Unsupported market. Use points, rebounds, assists, steals, or threes.")
    api_key = os.getenv("BETTINGPROS_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="BETTINGPROS_API_KEY is missing from this deployment's environment.")
    query = urlencode({"sport": "NBA", "market_id": market_id, "page": 1, "limit": 20})
    req = Request("https://api.bettingpros.com/v3/props?" + query, headers={"x-api-key": api_key, "accept": "application/json", "user-agent": "ColtsNBAEdge/1.0"}, method="GET")
    try:
        with urlopen(req, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as e:
        if e.code in (401, 403):
            detail = "BettingPros rejected the API key or this account tier does not allow this data."
        elif e.code == 429:
            detail = "BettingPros API request limit reached. Try again later."
        else:
            detail = "BettingPros API returned HTTP " + str(e.code) + "."
        raise HTTPException(status_code=502, detail=detail) from e
    except (URLError, TimeoutError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=502, detail="Could not retrieve valid data from BettingPros.") from e
    return JSONResponse({"market": market, "market_id": market_id, "props": payload.get("props", []) if isinstance(payload, dict) else [], "pagination": payload.get("_pagination", {}) if isinstance(payload, dict) else {}, "source": "BettingPros API"})

def _quarter_name_key(value):
    return "".join(ch.lower() for ch in str(value or "") if ch.isalnum())


def _enrich_live_quarters(payload):
    """Attach player quarter point totals only when ESPN supplies actual scoring plays."""
    if not isinstance(payload, dict):
        return payload
    games = payload.get("games") or []
    by_id = payload.get("players_by_id") or {}
    by_name = payload.get("players_by_name") or {}
    if not isinstance(by_id, dict) or not isinstance(by_name, dict):
        return payload
    player_objects = []
    seen = set()
    for collection in (by_id, by_name):
        for key, player in collection.items():
            if not isinstance(player, dict) or id(player) in seen:
                continue
            seen.add(id(player))
            player_objects.append((str(key), player))
    for game in games:
        if not isinstance(game, dict) or str(game.get("state") or "").lower() not in ("in", "post", "final"):
            continue
        event_id = game.get("event_id") or game.get("id")
        if not event_id:
            continue
        try:
            summary_url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?" + urlencode({"event": str(event_id)})
            req = Request(summary_url, headers={"User-Agent": "Mozilla/5.0 ColtNBAEdge/1.0", "Accept": "application/json"})
            with urlopen(req, timeout=5) as response:
                summary = json.loads(response.read().decode("utf-8", "replace"))
        except Exception:
            continue
        plays = summary.get("scoringPlays") or []
        if not plays:
            plays = (summary.get("plays") or {}).get("scoringPlays") or []
        totals_by_id = {}
        totals_by_name = {}
        for play in plays:
            if not isinstance(play, dict):
                continue
            period = play.get("period") or {}
            try:
                quarter = int(period.get("number") if isinstance(period, dict) else period)
                points = int(float(play.get("scoreValue") or 0))
            except (TypeError, ValueError):
                continue
            if quarter < 1 or quarter > 4 or points <= 0:
                continue
            scorer = play.get("athlete") or play.get("scorer")
            if isinstance(scorer, dict):
                pid = scorer.get("id")
                name = scorer.get("displayName") or scorer.get("fullName") or scorer.get("name")
            else:
                pid = play.get("athleteId") or play.get("scorerId")
                name = play.get("athleteName") or play.get("scorerName")
            if not pid and not name:
                participants = play.get("participants") or play.get("athletesInvolved") or []
                for part in participants:
                    if not isinstance(part, dict):
                        continue
                    role = str(part.get("role") or part.get("type") or "").lower()
                    athlete = part.get("athlete") if isinstance(part.get("athlete"), dict) else part
                    if "scor" in role or "shooter" in role:
                        scorer = athlete
                        pid = athlete.get("id")
                        name = athlete.get("displayName") or athlete.get("fullName") or athlete.get("name")
                        break
            if pid:
                row = totals_by_id.setdefault(str(pid), [0, 0, 0, 0])
                row[quarter - 1] += points
            elif name:
                row = totals_by_name.setdefault(_quarter_name_key(name), [0, 0, 0, 0])
                row[quarter - 1] += points
        if not totals_by_id and not totals_by_name:
            continue
        for key, player in player_objects:
            pid = str(player.get("athlete_id") or player.get("id") or key)
            name = player.get("player") or player.get("name") or player.get("displayName") or ""
            qpoints = totals_by_id.get(pid) or totals_by_name.get(_quarter_name_key(name))
            if qpoints is not None:
                player["points_by_quarter"] = qpoints
    return payload

@app.get("/api/model/diagnostics")
def model_diagnostics():
    """Safe database diagnostics; never returns connection strings or credentials."""
    import re
    result = {
        "database_url_configured": bool(os.getenv("DATABASE_URL")),
        "postgres_url_configured": bool(os.getenv("POSTGRES_URL")),
        "psycopg_importable": False,
        "connection_ok": False,
        "table_exists": False,
    }
    try:
        import psycopg
        result["psycopg_importable"] = True
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        result["error"] = str(exc)[:240]
        return JSONResponse(result, status_code=503)
    try:
        with prediction_ledger._connect() as conn:
            result["connection_ok"] = True
            row = conn.execute("SELECT to_regclass('public.nba_predictions')").fetchone()
            result["table_exists"] = bool(row and row[0])
    except Exception as exc:
        message = str(exc)
        message = re.sub(r'postgres(?:ql)?://[^\s]+', '[redacted database URL]', message, flags=re.I)
        result["error_type"] = type(exc).__name__
        result["error"] = message[:300]
        return JSONResponse(result, status_code=503)
    return JSONResponse(result)

@app.get("/api/model/ledger")
def model_ledger(status: str = "unsettled", market: str = "all", limit: int = 100):
    try:
        prediction_ledger.initialize()
        return JSONResponse({"predictions": prediction_ledger.list_predictions(status, market, limit)})
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Prediction database unavailable.") from exc

@app.get("/api/model/metrics")
def model_metrics(market: str = "all"):
    try:
        prediction_ledger.initialize()
        return JSONResponse(prediction_ledger.metrics(market))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Could not calculate calibration metrics.") from exc

@app.post("/api/model/settle")
async def model_settle(request: FastRequest):
    try:
        payload = await request.json()
        prediction_id = int(payload.get("prediction_id"))
        prediction_ledger.initialize()
        return JSONResponse(prediction_ledger.settle_prediction(prediction_id, payload.get("actual_value")))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Could not settle prediction.") from exc

@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def api_proxy(path: str, request: FastRequest):
    base = LIVE_UPSTREAM if path == "live-stats" else UPSTREAM
    url = base + "/api/" + path
    if request.url.query:
        url += "?" + request.url.query
    headers = {"accept": request.headers.get("accept", "application/json")}
    secret = os.getenv("VERCEL_AUTOMATION_BYPASS_SECRET")
    if secret:
        headers["x-vercel-protection-bypass"] = secret
    body = await request.body()
    if request.headers.get("content-type"):
        headers["content-type"] = request.headers["content-type"]
    req = Request(url, data=body if body else None, headers=headers, method=request.method)
    try:
        with urlopen(req, timeout=25) as response:
            response_body = response.read()
            media_type = response.headers.get("content-type", "application/json")
            if path == "auto-board" and "json" in media_type.lower():
                try:
                    board_payload = json.loads(response_body.decode("utf-8", "replace"))
                    prediction_ledger.initialize()
                    prediction_ledger.save_predictions(board_payload.get("props") or [], "points")
                except Exception:
                    pass
            if path == "live-stats" and "json" in media_type.lower():
                try:
                    live_payload = json.loads(response_body.decode("utf-8", "replace"))
                    live_payload = _enrich_live_quarters(live_payload)
                    response_body = json.dumps(live_payload, separators=(",", ":")).encode("utf-8")
                except Exception:
                    pass
            return Response(content=response_body, status_code=response.status, media_type=media_type)
    except HTTPError as e:
        return Response(content=e.read(), status_code=e.code, media_type=e.headers.get("content-type", "application/json"))
    except (URLError, TimeoutError) as e:
        raise HTTPException(status_code=502, detail="Upstream API unavailable") from e
