import {domain} from '../constants/domain.js';import {research} from '../stores/research.js';import {session} from '../stores/session.js';import {runsApi,sparesApi} from '../api/research.js';import {runOptions} from '../components/common/business.js';import {status} from '../components/common/status.js';import {esc,fmt,formObject} from '../utils/format.js';import {action,toast,bind} from '../hooks/actions.js';

const versionStates={computing:'两端计算中',published:'已发布',superseded:'已被新版取代',failed:'计算失败'};
const endStates={queued:'排队中',running:'计算中',completed:'已给出构成',infeasible:'明确无解',failed:'失败'};

function versionStatus(state){return '<span class="status '+esc(state==='published'?'completed':state==='computing'?'running':state)+'">'+esc(versionStates[state]||state)+'</span>'}

function percent(value){return (value*100).toLocaleString('zh-CN',{maximumFractionDigits:3})+'%'}

function allocationTable(result){
  const labels=result.labels;
  const head='<tr><th>真类 ＼ 判读去向</th>'+labels.map(l=>'<th>'+esc(l)+'</th>').join('')+'<th>真类件数</th></tr>';
  const body=result.allocation.map((row,i)=>'<tr><td>'+esc(labels[i])+'</td>'+row.map((v,j)=>'<td>'+v+'<small>'+percent(result.rates[i][j])+'</small></td>').join('')+'<td>'+result.true_totals[i]+'</td></tr>').join('');
  const foot='<tr><td>判读件数（列合计）</td>'+result.predicted_totals.map((v,j)=>'<td>'+v+(v===result.counts[j]?'':' ⚠')+'</td>').join('')+'<td>'+result.total+'</td></tr>';
  return '<div class="table-wrap"><table><thead>'+head+'</thead><tbody>'+body+'</tbody><tfoot>'+foot+'</tfoot></table></div>';
}

function endPanel(result){
  if(!result)return '';
  if(!result.feasible){
    return '<article class="spares-end infeasible"><header><h3>'+esc(result.end==='min'?'最少':'最多')+'</h3><span class="status failed">'+esc(endStates.infeasible)+'</span></header><p class="failure-reason">'+esc(result.reason)+'</p></article>';
  }
  return '<article class="spares-end"><header><h3>'+esc(result.end==='min'?'最少':'最多')+'</h3><span class="status completed">'+esc(endStates.completed)+'</span></header>'+
    '<dl class="metrics spares-metrics"><div><dt>'+esc(result.target_label)+'整件</dt><dd>'+result.target_count+'<small> 件 / 共 '+result.total+'</small></dd></div><div><dt>真实故障占比</dt><dd>'+percent(result.share)+'</dd></div></dl>'+
    '<p class="fine">'+(result.end==='min'?'最少':'最多')+'构成：真类件数 '+result.true_totals.join(' / ')+'。逐格为整件数，小字为该真类判读去向比例；每行合计等于真类件数，每列合计等于给定判读件数。</p>'+
    allocationTable(result)+'<p class="fine">'+esc(result.explanation)+'</p></article>';
}

function currentVersion(version){
  const matrix='<p class="fine">冻结折外混淆矩阵：真类 '+version.labels.map((l,i)=>esc(l)+' 行 '+version.matrix[i].join('/')).join('；')+' · 允许偏差 ±'+esc(version.tolerance)+' 个百分点 · 输入校验和 <code>'+esc(version.checksum.slice(0,16))+'</code></p>';
  const counts='判读件数：'+version.labels.map((l,i)=>esc(l)+' '+version.counts[i]+' 件').join(' · ');
  if(version.state==='computing'){
    const ends=Object.entries(version.ends).map(([k,v])=>esc(k==='min'?'最少':'最多')+' '+esc(endStates[v.state]||v.state)).join(' · ');
    return '<section class="spares-current"><h2>v'+version.version_no+' '+versionStatus(version.state)+'</h2><p>'+esc(counts)+' · 目标故障 '+esc(version.target_label)+'</p>'+matrix+'<div class="pending"><span class="spinner"></span><p>独立 worker 正在用该版冻结资料分别求两端：'+ends+'。同版两端齐备后才会在本页显示构成。</p></div></section>';
  }
  if(version.state==='failed'){
    return '<section class="spares-current"><h2>v'+version.version_no+' '+versionStatus(version.state)+'</h2><p>'+esc(counts)+'</p>'+matrix+'<div class="failure"><h3>该版未能给出结论</h3><p>'+esc(version.failure_reason||'计算中断，尚未完成。')+'</p>'+(session.writable?'<button id="retry-spares" class="secondary">用同一版资料重跑未完成端 ↻</button>':'')+'</div></section>';
  }
  const results=version.results||{};
  const ends=Object.entries(version.ends).map(([k,v])=>esc(k==='min'?'最少':'最多')+' '+esc(endStates[v.state]||v.state)).join(' · ');
  return '<section class="spares-current"><h2>v'+version.version_no+' '+versionStatus(version.state)+'</h2><p>'+esc(counts)+' · 目标故障 '+esc(version.target_label)+'</p>'+matrix+'<p class="fine">两端状态：'+ends+'。'+(version.published_at?'发布于 '+esc(version.published_at):'')+'</p><div class="spares-ends">'+endPanel(results.min)+endPanel(results.max)+'</div></section>';
}

function history(versions){
  if(versions.length<2)return '';
  return '<details class="spares-history"><summary>版本记录 '+versions.length+' 版 · 更正判读件数会另开新版，旧计算不会覆盖新版</summary><div class="ledger">'+versions.map(v=>'<div class="ledger-row"><small>v'+v.version_no+'</small><strong>目标 '+esc(v.target_label)+' · ±'+esc(v.tolerance)+' 点 · '+v.counts.reduce((a,b)=>a+b,0)+' 件</strong>'+versionStatus(v.state)+'<time>'+esc(v.created_by)+'</time></div>').join('')+'</div></details>';
}

export async function sparesPage(){
  const completed=research.runs.filter(r=>r.state==='completed');
  const runId=completed.some(r=>r.id===research.runId)?research.runId:completed[0]?.id;
  let data=null;
  if(runId)try{data=await sparesApi.get(runId)}catch(e){data={error:e.message}}
  const labelSource=(data?.versions||[]).map(v=>v.labels).find(Boolean)||data?.labels||[];
  research.activeSpares=data;
  const head='<section class="page-heading"><p class="eyebrow">05 / SPARES INTERVAL</p><h1>'+esc(domain.spares_headline)+'</h1><p>冻结已完成研究的折外混淆矩阵、判读件数与允许偏差；每个误判率与判对率在原值上下浮动给定百分点，同一真类的判读去向比例相加仍为100%。按整件求指定故障的最少与最多构成，两个极值都能复原判读计数。</p></section>';
  if(!completed.length)return head+'<p class="empty">还没有已完成的研究。先在分析配置中执行一轮计算，完成后再做备件反演。</p>';
  const selector='<label class="compact">已完成研究<select id="spares-run">'+completed.map(r=>'<option value="'+r.id+'" '+(r.id===runId?'selected':'')+'>'+esc(r.name)+' · #'+r.id+'</option>').join('')+'</select></label>';
  if(data?.error)return head+selector+'<div class="failure"><h2>读取失败</h2><p>'+esc(data.error)+'</p></div>';
  const current=data?.versions?.find(v=>v.current)||null;
  const form=(session.writable&&runId)?'<form id="spares-form" class="entry-sheet"><h2>冻结一版判读件数</h2><label>目标故障<select name="target_label">'+labelSource.map(l=>'<option value="'+esc(l)+'" '+(l===current?.target_label?'selected':'')+'>'+esc(l)+'</option>').join('')+'</select></label><label>允许偏差 ±<input name="tolerance" type="number" step="0.1" min="0" max="100" required value="'+(current?esc(current.tolerance):'5')+'"> 个百分点</label><div class="parameter-fields">'+labelSource.map((l,i)=>'<label>'+esc(l)+' 判读件数<input type="number" min="0" step="1" required name="count:'+esc(l)+'" value="'+(current?current.counts[i]:'')+'"></label>').join('')+'</div><p class="fine">判读件数指新批各判读去向的件数，合计至少1件。提交即冻结为新版本；即使只是更正件数也会另开一版。</p><button type="submit">冻结并求两端 →</button></form>':'<aside class="entry-sheet"><h2>阅读权限</h2><p>阅读者可查看已发布版本的最少/最多构成。冻结版本与更正判读件数由分析师或负责人执行。</p></aside>';
  const empty=current?'':'<p class="empty">该研究还没有备件版本。'+(session.writable?'在右侧冻结一版判读件数，独立 worker 会分别求最少与最多构成。':'等待分析师冻结第一版。')+'</p>';
  return head+selector+'<div class="split spares-layout"><section>'+(current?currentVersion(current):empty)+history(data?.versions||[])+'</section>'+form+'</div>';
}

export function bindSpares(refresh){
  bind('#spares-run','change',async e=>{research.runId=Number(e.target.value);await refresh()});
  action('#spares-form',async form=>{
    const data=formObject(form),counts={};
    for(const [key,value]of Object.entries(data))if(key.startsWith('count:'))counts[key.slice(6)]=Number(value);
    await sparesApi.create(research.runId,{target_label:data.target_label,tolerance:String(data.tolerance),counts});
    await refresh();toast('已冻结新版本，独立 worker 将分别求最少与最多');
  });
  bind('#retry-spares','click',async()=>{try{await sparesApi.retry(research.runId);await refresh();toast('已用同一版资料重新排队未完成端')}catch(e){toast(e.message,true)}});
  const current=research.activeSpares?.versions?.find(v=>v.current);
  if(current&&current.state==='computing')setTimeout(refresh,1500);
}
