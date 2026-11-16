'use strict';
// Pure state/HTML-string smoke checks with DOM stubs; does not launch a browser.
const fs=require('node:fs'),vm=require('node:vm'),crypto=require('node:crypto'),assert=require('node:assert/strict'),path=require('node:path');
const root=path.resolve(__dirname,'..'),elements=new Map(),listeners={},timeouts=[],intervals=new Map();
let nextTimer=1;
function element(id){
 if(!elements.has(id))elements.set(id,{id,value:'',checked:false,open:false,hidden:false,innerHTML:'',textContent:'',isConnected:true,dataset:{},classList:{toggle(){}},
  focus(){},addEventListener(name,fn){this[name]=fn},showModal(){this.open=true},close(){this.open=false},querySelector(){return null}});
 return elements.get(id);
}
const document={activeElement:null,getElementById:element,querySelectorAll:()=>[],querySelector:()=>null,createElement:()=>element('file'),
 addEventListener(name,fn){(listeners[name]??=[]).push(fn)}};
const context=vm.createContext({document,window:{addEventListener(){}},location:{hash:'#buyer'},crypto,URL,console,
 setTimeout(fn){timeouts.push(fn);return timeouts.length},clearTimeout(){},setInterval(fn){const id=nextTimer++;intervals.set(id,fn);return id},clearInterval(id){intervals.delete(id)}});
vm.runInContext(fs.readFileSync(path.join(root,'ui/app.js'),'utf8'),context,{filename:'app.js'});
function evaluate(code){return vm.runInContext(code,context)}
function field(id,value,checked=false){Object.assign(element(id),{value,checked})}
function command(name,data={}){context.testElement={dataset:data};evaluate('commands['+JSON.stringify(name)+'](testElement)')}
function event(name,target){for(const listener of listeners[name]||[])listener({target,preventDefault(){}})}
function flushTimeouts(){const pending=timeouts.splice(0);for(const fn of pending)fn()}
function reset(){evaluate('initial();scenario="normal";clearInterval(timer);s.generating=false;location.hash="#buyer";render()');intervals.clear();timeouts.length=0}
const results=[];
function test(name,fn){try{reset();fn();results.push({name,passed:true})}catch(error){results.push({name,passed:false,detail:error.message})}}

test('八个原型入口能够生成页面内容',()=>{
 for(const route of ['buyer','checkout','orders','staff','knowledge','models','governance','operations']){
  context.testRoute=route;evaluate('location.hash="#"+testRoute;render()');
  assert(element('app').innerHTML.length>600,route);
  assert(!element('app').innerHTML.includes('undefined'),route);
  assert(!element('app').innerHTML.includes('NaN'),route);
 }
});
test('商品与规格参数保留在原确认卡片',()=>{
 command('sku',{alt:'1'});field('sku-choice','月光白 / 256GB');field('sku-quantity','2');command('prepare-cart');
 assert.equal(evaluate('s.action.name'),'轻享手机 B2');assert.equal(evaluate('s.action.price'),279900);
 evaluate('globalThis.firstAction=s.action;s.sku="其他规格";s.quantity=5');
 assert.equal(evaluate('firstAction.sku'),'月光白 / 256GB');assert.equal(evaluate('firstAction.quantity'),2);
 command('sku');field('sku-choice','冰川蓝 / 256GB');field('sku-quantity','1');command('prepare-cart');
 assert.equal(evaluate('firstAction.state'),'STALE');assert.notEqual(evaluate('s.action.id'),evaluate('firstAction.id'));
});
test('异步结果只更新原操作编号，重复点击不重复执行',()=>{
 command('sku');field('sku-choice','冰川蓝 / 256GB');field('sku-quantity','1');command('prepare-cart');
 const id=evaluate('s.action.id');command('action-confirm',{id});command('action-confirm',{id});assert.equal(timeouts.length,1);
 evaluate('globalThis.executing=s.action');
 command('sku',{alt:'1'});field('sku-choice','月光白 / 256GB');field('sku-quantity','3');command('prepare-cart');
 flushTimeouts();assert.equal(evaluate('executing.state'),'SUCCEEDED');assert.equal(evaluate('s.action.state'),'PENDING_CONFIRMATION');
});
test('未知执行结果保留原卡且不提供再次确认',()=>{
 command('sku');field('sku-choice','冰川蓝 / 256GB');field('sku-quantity','1');command('prepare-cart');
 evaluate('scenario="offline"');command('action-confirm',{id:evaluate('s.action.id')});
 assert.equal(evaluate('s.action.state'),'UNKNOWN');assert(!evaluate('actionCard(s.action)').includes('data-cmd="action-confirm"'));
});
test('领取需要有效请求，接管使待确认卡失效',()=>{
 command('claim');assert.equal(evaluate('s.conversation'),'AI_ACTIVE');
 command('sku');field('sku-choice','冰川蓝 / 256GB');field('sku-quantity','1');command('prepare-cart');
 command('handoff');command('claim');assert.equal(evaluate('s.conversation'),'HUMAN_ACTIVE');assert.equal(evaluate('s.action.state'),'STALE');
 assert.equal(evaluate('s.messages.at(-1).staffName'),'小林');
 evaluate('s.staff="小周"');assert.equal(evaluate('s.messages.at(-1).staffName'),'小林');
});
test('内部备注与会话历史隔离',()=>{
 command('handoff');command('claim');field('note-input','内部备注专用内容');event('submit',{id:'note-form'});
 assert(evaluate('staff()').includes('内部备注专用内容'));assert(!evaluate('buyer()').includes('内部备注专用内容'));
 const count=evaluate('s.messages.length');command('select-history',{id:'2'});assert.equal(evaluate('s.messages.length'),2);assert.equal(evaluate('s.notes.length'),0);
 command('select-history',{id:'1'});assert.equal(evaluate('s.messages.length'),count);assert.equal(evaluate('s.notes.length'),1);
});
test('知识草稿与发布不改写历史引用原文',()=>{
 const old=evaluate('s.kbVersions[1]');
 evaluate('s.kbText="新版服务规则";s.kbDraft="READY"');command('kb-publish-execute');
 assert.equal(evaluate('s.kbActive'),2);assert.equal(evaluate('s.kbVersions[1]'),old);assert.equal(evaluate('s.kbVersions[2]'),'新版服务规则');
 command('source',{version:'1'});assert(element('modal-body').innerHTML.includes('历史引用版本'));assert(element('modal-body').innerHTML.includes('自营演示商城'));
 command('kb-toggle');command('source',{version:'1'});assert(element('modal-body').innerHTML.includes('已停用'));
});
test('知识构建失败保留旧活动版本',()=>{
 evaluate('scenario="failed"');command('kb-build');
 for(let n=0;n<4;n++)for(const fn of [...intervals.values()])fn();
 assert.equal(evaluate('s.kbDraft'),'FAILED');assert.equal(evaluate('s.kbActive'),1);
 command('kb-publish-execute');assert.equal(evaluate('s.kbActive'),1);
});
test('留存配置试算后值保留，修改后试算失效',()=>{
 event('change',{id:'retention-text',value:'45'});command('retention-preview');
 assert.equal(evaluate('s.retention.text'),45);assert(evaluate('governance()').includes('value="45"'));assert.equal(evaluate('s.retentionPreview'),true);
 event('change',{id:'retention-audio',value:'8'});assert.equal(evaluate('s.retentionPreview'),false);
});
test('报价参数变更要求重新报价',()=>{
 event('change',{id:'checkout-quantity',value:'2'});assert.equal(evaluate('s.checkoutQuoted'),false);
 assert.equal(evaluate('quoteValues().subtotal'),599800);command('requote');assert.equal(evaluate('s.checkoutQuoted'),true);
});
test('退货确认显示已选订单明细、原因与凭证',()=>{
 command('return-prepare',{order:'84'});field('return-quantity','1');field('return-reason','商品问题');field('return-description','演示说明');field('return-proof','',true);
 command('return-card');
 const content=element('modal-body').innerHTML;assert(content.includes('8401'));assert(content.includes('商品问题'));assert(content.includes('1份演示图片'));assert(content.includes('演示说明'));
 command('return-submit');assert.equal(evaluate('s.returnSubmitted'),true);assert.equal(evaluate('s.returnDraft.order'),'84');
});
test('售后阶段区分批准、收货与模拟退款',()=>{
 field('approved-amount','2899');command('return-approved');assert.equal(evaluate('s.returnStage'),'APPROVED');
 command('return-received');assert.equal(evaluate('s.returnStage'),'GOODS_RECEIVED');
 command('return-refunded');assert.equal(evaluate('s.returnStage'),'SIMULATED_REFUNDED');
});
test('不合理物流时间不写入演示轨迹',()=>{
 const count=evaluate('s.nodes.length');field('node-time','2026-11-01T10:00');field('node-place','演示地点');field('node-description','旧时间');command('node-save');
 assert.equal(evaluate('s.nodes.length'),count);
});
const report={date:'2026-11-11',kind:'pure-javascript-state-smoke-check',passed:results.every(r=>r.passed),checks:results,
 limits:['使用最小 DOM 存根，不是真实浏览器检查','未验证视觉布局、响应式和原生弹窗焦点','未访问 API 或数据库']};
fs.writeFileSync(path.join(root,'prototype-validation.json'),JSON.stringify(report,null,2)+'\n','utf8');
console.log(JSON.stringify(report,null,2));
process.exitCode=report.passed?0:1;
