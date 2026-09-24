"""Self-contained preview dashboard for the fuel optimiser, served by the API at
GET /. Same-origin fetches to /api/fuel|optimizer|savings — no build step, meant
as a quick live look while the real CCR-frontend panel is built."""

HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CCR Fuel Optimiser — live advisory</title>
<style>
:root{--bg:#0d1017;--surf:#151a23;--surf2:#1c222d;--ink:#e8ecf4;--mut:#7b8496;
--sec:#b3bbcc;--hair:#262d3a;--blue:#5b8cff;--green:#4fce9e;--amber:#f0b45e;--red:#f16a6a;
--mono:ui-monospace,"SF Mono",Menlo,monospace}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;font-size:15px}
.wrap{max-width:1080px;margin:0 auto;padding:20px 22px 80px}
header{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;border-bottom:1px solid var(--hair);padding-bottom:14px}
h1{font-size:19px;margin:0;font-weight:650;letter-spacing:-.01em}
.dot{width:8px;height:8px;border-radius:50%;background:var(--green);display:inline-block;box-shadow:0 0 8px rgba(79,206,158,.6)}
.pill{font-family:var(--mono);font-size:10.5px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;padding:3px 8px;border-radius:5px}
.pill.adv{background:rgba(240,180,94,.14);color:var(--amber)}
.pill.est{background:rgba(123,132,150,.16);color:var(--mut)}
.upd{margin-left:auto;font-family:var(--mono);font-size:12px;color:var(--mut)}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--mut);margin:30px 0 12px}
.kpis{display:grid;grid-template-columns:repeat(5,1fr);gap:10px}
@media(max-width:760px){.kpis{grid-template-columns:repeat(2,1fr)}}
.kpi{background:var(--surf);border:1px solid var(--hair);border-radius:11px;padding:14px;border-top:3px solid var(--kc,var(--blue))}
.kpi .n{font-family:var(--mono);font-size:23px;font-weight:600;line-height:1;color:var(--kc,var(--ink));font-variant-numeric:tabular-nums}
.kpi .l{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);margin-top:8px}
.kpi .s{font-size:11.5px;color:var(--sec);margin-top:4px;font-family:var(--mono)}
.card{background:var(--surf);border:1px solid var(--hair);border-radius:12px;padding:18px 20px}
.row{display:flex;gap:22px;flex-wrap:wrap;align-items:center}
.big{font-family:var(--mono);font-size:22px;font-weight:600;font-variant-numeric:tabular-nums}
.arrow{color:var(--mut);margin:0 4px}
.chip{font-size:11.5px;font-weight:600;padding:3px 9px;border-radius:6px}
.ok{background:rgba(79,206,158,.15);color:var(--green)}
.warn{background:rgba(240,180,94,.15);color:var(--amber)}
.bad{background:rgba(241,106,106,.15);color:var(--red)}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:6px}
th{text-align:left;font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);font-weight:500;padding:6px 8px;border-bottom:1px solid var(--hair)}
td{padding:7px 8px;border-bottom:1px solid var(--hair);font-family:var(--mono);font-variant-numeric:tabular-nums}
td.name{font-family:system-ui;color:var(--sec)}
.pos{color:var(--green)}.neg{color:var(--red)}
.expl{margin:12px 0 0;padding-left:18px;color:var(--sec);font-size:13px;line-height:1.6}
.note{font-size:12px;color:var(--mut);margin-top:14px;line-height:1.5}
</style></head><body><div class="wrap">
<header>
  <span class="dot"></span><h1>CCR Fuel Optimiser</h1>
  <span class="pill adv">Advisory only</span>
  <span class="pill est" id="estpill" style="display:none">Estimated economics</span>
  <span class="upd" id="upd">connecting…</span>
</header>

<h2>Live fuel scoreboard</h2>
<div class="kpis" id="kpis"></div>

<h2>Current advisory <span id="modewrap" style="text-transform:none;color:var(--sec)"></span></h2>
<div class="card" id="advisory">loading…</div>

<h2>What drives what — per-lever sensitivity</h2>
<div class="card"><table id="sens"><thead><tr><th>Fuel lever</th><th>Temp pull</th><th>Δ cost / unit</th><th>Δ TSR / unit</th></tr></thead><tbody></tbody></table>
<div class="note" id="sensnote"></div></div>

<h2>Measurement &amp; verification</h2>
<div class="card" id="mv">loading…</div>

<p class="note">Nimbahera Kiln 3 calciner. Predicts 5&nbsp;min ahead; recommends min-₹/ton coal+AFR mix subject to the temperature band, safety interlocks, and predicted clinker quality. Nothing here writes to the plant. Economics are estimated defaults until real procurement/QC values are supplied. Auto-refreshes every 15&nbsp;s.</p>
</div>
<script>
const $=s=>document.querySelector(s);
const f1=v=>v==null?"—":(+v).toFixed(1), f2=v=>v==null?"—":(+v).toFixed(2), f0=v=>v==null?"—":Math.round(v);
const sign=v=>v==null?"":(v>0?"+":"");
function kpi(kc,n,l,s){return `<div class="kpi" style="--kc:${kc}"><div class="n">${n}</div><div class="l">${l}</div><div class="s">${s||""}</div></div>`}
async function jget(u){try{const r=await fetch(u);return r.ok?await r.json():null}catch(e){return null}}
async function tick(){
  const [fuel,opt,sav]=await Promise.all([jget('/api/fuel'),jget('/api/optimizer'),jget('/api/savings')]);
  $('#upd').textContent = new Date().toLocaleTimeString();
  if(!fuel||!fuel.available){$('#kpis').innerHTML='<div class="kpi"><div class="n">—</div><div class="l">optimizer loop not logged yet</div></div>';return}
  $('#estpill').style.display = fuel.is_estimate?'inline-block':'none';
  const c=fuel.current, r=fuel.recommended, s=fuel.savings;
  const bandChip = fuel.temp_in_band?'<span class="chip ok">in band</span>':'<span class="chip warn">out of band</span>';
  $('#kpis').innerHTML =
    kpi('#5b8cff', f1(c.cost_per_ton), '₹ / ton clinker', `advisory ${f1(r.cost_per_ton)}`)+
    kpi('#4fce9e', f1(c.tsr_pct)+'%', 'Thermal substitution', `advisory ${f1(r.tsr_pct)}%`)+
    kpi('#f0b45e', f2(c.coal_tph), 'Calciner coal t/h', `advisory ${f2(r.coal_tph)}`)+
    kpi('#b3bbcc', f0(c.afr_total), 'AFR total', `advisory ${f0(r.afr_total)}`)+
    kpi('#7b8496', f0(c.specific_heat_kcal_kg), 'kcal / kg clinker', `advisory ${f0(r.specific_heat_kcal_kg)}`);
  $('#modewrap').textContent = ' · mode: '+(fuel.mode||'—');
  const saveCls=s.cost_per_ton>0?'pos':'neg';
  const q = fuel.quality_ok?'<span class="chip ok">quality in spec</span>':'<span class="chip bad">quality out of spec</span>';
  const il = fuel.interlocks_ok?'<span class="chip ok">interlocks ok</span>':'<span class="chip bad">interlock issue</span>';
  $('#advisory').innerHTML =
    `<div class="row"><div><span class="big">${f2(c.coal_tph)}</span><span class="arrow">→</span><span class="big" style="color:var(--amber)">${f2(r.coal_tph)}</span><div class="l" style="font-size:11px;color:var(--mut)">coal t/h</div></div>`+
    `<div><span class="big">${f0(c.afr_total)}</span><span class="arrow">→</span><span class="big" style="color:var(--green)">${f0(r.afr_total)}</span><div class="l" style="font-size:11px;color:var(--mut)">AFR total</div></div>`+
    `<div><span class="big ${saveCls}">${sign(s.cost_per_ton)}${f1(s.cost_per_ton)}</span><div class="l" style="font-size:11px;color:var(--mut)">₹/ton saved</div></div>`+
    `<div><span class="big">${sign(s.tsr_pts)}${f2(s.tsr_pts)}</span><div class="l" style="font-size:11px;color:var(--mut)">TSR pts</div></div>`+
    `<div style="display:flex;gap:8px;flex-wrap:wrap">${bandChip}${q}${il}</div></div>`;
  // sensitivities
  const sens=(opt&&opt.correlation&&opt.correlation.sensitivities)||{};
  const names={'467RL400_TPH':'Calciner coal','LAFR_FT':'Liquid AFR (PC)','K3V10_F1':'Liquid AFR (TAD)','261WF080_TPH':'Solid AFR'};
  $('#sens tbody').innerHTML = Object.keys(names).map(t=>{const v=sens[t]||{};
    return `<tr><td class="name">${names[t]}</td><td>${v.temp_pull_c==null?'—':f1(v.temp_pull_c)+' °C'}</td><td>${v.d_cost_per_unit==null?'—':'₹'+f1(v.d_cost_per_unit)}</td><td>${v.d_tsr_per_unit==null?'—':f2(v.d_tsr_per_unit)}</td></tr>`}).join('');
  $('#sensnote').textContent='Per +1 unit of each lever, from the twin (temperature) and the economics (cost/TSR). Coal moves temperature; AFR moves TSR at lower cost.';
  // M&V
  if(sav&&sav.available){const rf=sav.realized_followed_avg||{},pa=sav.predicted_avg||{};
    $('#mv').innerHTML=`<div class="row">`+
      `<div><span class="big">${sav.n}</span><div class="l" style="font-size:11px;color:var(--mut)">advisories logged</div></div>`+
      `<div><span class="big">${sav.matured}</span><div class="l" style="font-size:11px;color:var(--mut)">matured</div></div>`+
      `<div><span class="big">${sav.followed}</span><div class="l" style="font-size:11px;color:var(--mut)">operator followed</div></div>`+
      `<div><span class="big">${pa.cost_per_ton==null?'—':sign(pa.cost_per_ton)+f1(pa.cost_per_ton)}</span><div class="l" style="font-size:11px;color:var(--mut)">avg predicted ₹/ton</div></div>`+
      `<div><span class="big">${rf.cost_per_ton==null?'—':sign(rf.cost_per_ton)+f1(rf.cost_per_ton)}</span><div class="l" style="font-size:11px;color:var(--mut)">avg realised ₹/ton (followed)</div></div></div>`+
      `<div class="note">Realised savings populate as advisories age past the ${8}-min operator-reaction window and get matured against what the operator actually did.</div>`;
  } else $('#mv').innerHTML='<div class="note">No matured advisories yet — check back after a few minutes of live ticks.</div>';
}
tick(); setInterval(tick,15000);
</script></body></html>"""
