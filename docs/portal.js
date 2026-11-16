'use strict';
const spec=JSON.parse(document.getElementById('api-spec').textContent);
const escapeHtml=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const operations=[];
Object.entries(spec.paths).forEach(([path,methods])=>Object.entries(methods).forEach(([method,op])=>{if(op.operationId)operations.push({path,method,...op})}));
function deref(o){if(!o?.$ref)return o;return o.$ref.split('/').slice(1).reduce((a,k)=>a?.[k.replace(/~1/g,'/').replace(/~0/g,'~')],spec)}
function typeOf(schema){
 if(!schema)return '';
 if(schema.$ref)return schema.$ref.split('/').pop();
 if(schema.oneOf)return 'oneOf: '+schema.oneOf.map(typeOf).join(' | ');
 if(schema.anyOf)return schema.anyOf.map(typeOf).join(' | ');
 if(schema.allOf)return 'allOf: '+schema.allOf.map(typeOf).join(' & ');
 if(schema.type==='array')return 'array<'+typeOf(schema.items)+'>';
 return Array.isArray(schema.type)?schema.type.join(' | '):schema.type||'object';
}
function schemaTable(schema){
 const resolved=deref(schema);
 if(!resolved)return '';
 if(!resolved.properties)return '<pre><code>'+escapeHtml(JSON.stringify(resolved,null,2))+'</code></pre>';
 const req=resolved.required||[];
 return '<div class="table-scroll"><table><thead><tr><th>字段</th><th>类型</th><th>必填</th><th>规则 / 说明</th></tr></thead><tbody>'+Object.entries(resolved.properties).map(([key,p])=>'<tr><td class="schema-name">'+escapeHtml(key)+'</td><td>'+escapeHtml(typeOf(p))+'</td><td>'+(req.includes(key)?'是':'否')+'</td><td>'+escapeHtml([p.description,p.format&&'格式 '+p.format,p.enum&&p.enum.join(' / '),p.pattern&&'pattern: '+p.pattern,p.minimum!=null&&'最小 '+p.minimum,p.maximum!=null&&'最大 '+p.maximum,p.maxLength&&'最长 '+p.maxLength].filter(Boolean).join('；'))+'</td></tr>').join('')+'</tbody></table></div>';
}
function endpointBody(op){
 const params=(op.parameters||[]).map(deref),req=deref(op.requestBody),contents=req?.content||{};
 let out='<p><strong>'+escapeHtml(op.operationId)+'</strong> · '+escapeHtml(op['x-requirements'].join(' / '))+' · '+escapeHtml(op['x-ui-pages'].join(' / '))+'</p><p>'+escapeHtml(op.description)+'</p><p class="small-note">鉴权：'+escapeHtml((op.security||[]).map(x=>Object.keys(x).join(' + ')).join(' 或 '))+'</p>';
 if(params.length)out+='<h3>路径、查询与请求头</h3><div class="table-scroll"><table><thead><tr><th>参数</th><th>位置</th><th>必填</th><th>类型与说明</th></tr></thead><tbody>'+params.map(p=>'<tr><td>'+escapeHtml(p.name)+'</td><td>'+p.in+'</td><td>'+(p.required?'是':'否')+'</td><td>'+escapeHtml(typeOf(p.schema)+' · '+(p.description||p.schema?.description||''))+'</td></tr>').join('')+'</tbody></table></div>';
 Object.entries(contents).forEach(([mime,c])=>{out+='<h3>请求体 · '+escapeHtml(mime)+'</h3>'+schemaTable(c.schema)+'<details><summary>查看请求 Schema</summary><pre><code>'+escapeHtml(JSON.stringify(deref(c.schema),null,2))+'</code></pre></details>'});
 out+='<h3>成功响应</h3>';
 Object.entries(op.responses).filter(([status])=>/^2/.test(status)).forEach(([status,r])=>{r=deref(r);out+='<p><strong>HTTP '+status+'</strong> · '+escapeHtml(r.description)+'</p>';Object.entries(r.content||{}).forEach(([mime,c])=>{out+='<p class="small-note">'+escapeHtml(mime)+' · '+escapeHtml(typeOf(c.schema))+'</p>'+schemaTable(c.schema)})});
 const refs=new Set(),queue=[];
 function discover(value){
  if(Array.isArray(value))return value.forEach(discover);
  if(value&&typeof value==='object'){if(value.$ref?.startsWith('#/components/schemas/')&&!refs.has(value.$ref)){refs.add(value.$ref);queue.push(value.$ref)}Object.values(value).forEach(discover)}
 }
 discover(op);
 for(let i=0;i<queue.length&&i<40;i++)discover(deref({$ref:queue[i]}));
 out+='<h3>相关数据结构</h3><p class="small-note">展开查看具体字段与原始约束；嵌套类型可在下列同名结构中查阅。</p>';
 for(const ref of queue.slice(0,40)){const name=ref.split('/').pop(),schema=deref({$ref:ref});out+='<details><summary class="schema-name">'+escapeHtml(name)+'</summary>'+schemaTable(schema)+'<pre><code>'+escapeHtml(JSON.stringify(schema,null,2))+'</code></pre></details>'}
 out+='<details><summary>查看此接口完整 OpenAPI 定义</summary><pre><code>'+escapeHtml(JSON.stringify(op,null,2))+'</code></pre></details>';
 return out;
}
function renderApi(){
 const q=document.getElementById('api-search').value.trim().toLowerCase(),scope=document.getElementById('api-scope').value;
 const filtered=operations.filter(op=>{const bridge=op.path.includes('agent-bridge');return (!scope||(scope==='bridge'?bridge:!bridge&&op.path.includes('/'+scope+'/')))&&[op.path,op.summary,op.operationId,...op['x-requirements']].join(' ').toLowerCase().includes(q)});
 document.getElementById('api-count').textContent='共 '+filtered.length+' / '+operations.length+' 个接口 · 展开查看契约';
 document.getElementById('api-list').innerHTML=filtered.length?filtered.map(op=>'<details class="endpoint" data-id="'+op.operationId+'"><summary><span class="method '+op.method+'">'+op.method.toUpperCase()+'</span><span class="ep-path">'+escapeHtml(op.path)+'</span><span class="ep-title">'+escapeHtml(op.summary)+'</span></summary><div class="endpoint-body"></div></details>').join(''):'<div class="no-results">没有匹配的接口，试试其他名称或需求编号。</div>';
 document.querySelectorAll('.endpoint').forEach(el=>el.addEventListener('toggle',()=>{if(el.open&&!el.dataset.loaded){el.querySelector('.endpoint-body').innerHTML=endpointBody(operations.find(op=>op.operationId===el.dataset.id));el.dataset.loaded='1'}}));
}
function navigate(){
 const hash=location.hash.slice(1)||'overview',key=document.querySelector('[data-section="'+CSS.escape(hash)+'"]')?hash:'overview';
 document.body.dataset.page=key;
 document.querySelectorAll('[data-section]').forEach(el=>{el.hidden=el.dataset.section!==key});
 document.querySelectorAll('[data-nav]').forEach(el=>el.classList.toggle('active',el.dataset.nav===key));
 if(key==='api')renderApi();
 window.scrollTo({top:0,behavior:'instant'});
}
document.getElementById('api-search').addEventListener('input',renderApi);
document.getElementById('api-scope').addEventListener('change',renderApi);
document.querySelectorAll('[data-anchor]').forEach(button=>button.addEventListener('click',()=>button.closest('.doc-section').querySelector('#'+button.dataset.anchor).scrollIntoView({behavior:'smooth',block:'start'})));
window.addEventListener('hashchange',navigate);
navigate();
