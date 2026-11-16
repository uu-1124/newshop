import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import vm from 'node:vm'
import test from 'node:test'
import { parse, compileScript } from '@vue/compiler-sfc'
import ts from 'typescript'
const nativeRequire=createRequire(import.meta.url)
const deferred=()=>{let resolve;const promise=new Promise(done=>{resolve=done});return{promise,resolve}}
async function component(file,props,agent,audioServices={},globals={}) {
  const source=await readFile(new URL('../src/pages/agent/'+file,import.meta.url),'utf8')
  const compiled=compileScript(parse(source).descriptor,{id:file})
  const javascript=ts.transpileModule(compiled.content,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText
  const hooks={}, emitted=[], module={exports:{}}
  let sequence=0
  class AgentError extends Error{constructor(message,status,code){super(message);this.status=status;this.code=code}}
  vm.runInNewContext(javascript,{module,exports:module.exports,console,Blob,AbortController,JSON,Date,setTimeout,clearTimeout,setInterval:()=>0,clearInterval(){},
    require(name){
      if(name==='vue')return{defineComponent:x=>x,ref:value=>({value}),onMounted:fn=>{hooks.mount=fn},onBeforeUnmount:fn=>{hooks.unmount=fn}}
      if(name==='@/services/agent')return{agent,agentId:()=>`key-${++sequence}`,AgentError}
      if(name==='@/services/agentAudio')return audioServices
      return nativeRequire(name)
    },...globals})
  const state=module.exports.default.setup(props,{expose(){},emit:(name,value)=>emitted.push([name,value])})
  return{state,hooks,emitted}
}
test('transcription keeps text editable and emits only after explicit transcription',async()=>{
  let uploads=0,transcriptions=0,sends=0
  const h=await component('VoiceComposer.vue',{disabled:false},{
    upload:async()=>{uploads++;return{id:'recording'}},transcribe:async()=>{transcriptions++;return{text:'可编辑的转写'}},send:()=>{sends++}
  },{startRecording:async()=>({finish:async()=>new Blob(['wav']),cancel:async()=>{}})})
  await h.state.start();assert.equal(h.state.recording.value,true)
  await h.state.finish();assert.equal(h.state.blob.value.size,3);assert.equal(uploads,0)
  await h.state.transcribe();assert.equal(uploads,1);assert.equal(transcriptions,1);assert.equal(sends,0)
  assert.equal(h.emitted[0][0],'transcribed');assert.equal(h.emitted[0][1],'可编辑的转写')
  assert.match(h.state.status.value,/手动发送/)
})
test('cancelled microphone request and late transcription cannot leak into another conversation',async()=>{
  const pending=deferred(),requested=deferred();let cancelled=0
  const h=await component('VoiceComposer.vue',{disabled:false},{},{startRecording:()=>{requested.resolve();return pending.promise}})
  const opening=h.state.start();await requested.promise;await h.state.cancel()
  pending.resolve({cancel:async()=>{cancelled++}});await opening
  assert.equal(cancelled,1);assert.equal(h.state.recording.value,false)
  const transcript=deferred()
  const second=await component('VoiceComposer.vue',{disabled:false},{upload:async()=>({id:'recording'}),transcribe:()=>transcript.promise},
    {startRecording:async()=>({finish:async()=>new Blob(['wav']),cancel:async()=>{}})})
  await second.state.start();await second.state.finish()
  const working=second.state.transcribe();await Promise.resolve();await Promise.resolve();await second.state.cancel()
  transcript.resolve({text:'不应写入新会话'});await working
  assert.equal(second.emitted.length,0)
})
test('microphone denial leaves a clear text-input fallback',async()=>{
  const h=await component('VoiceComposer.vue',{disabled:false},{},{startRecording:async()=>{throw new Error('未授予麦克风权限，可继续输入文字')}})
  await h.state.start();assert.equal(h.state.recording.value,false);assert.match(h.state.status.value,/文字/)
})
test('feedback loads prior vote and retries a failed save with the same logical key',async()=>{
  const attempts=[]
  const h=await component('AnswerFeedback.vue',{messageId:'answer'},{feedback:async()=>({vote:-1,reason:'缺少依据'}),
    submitFeedback:async(...args)=>{attempts.push(args);if(attempts.length===1)throw new Error('网络中断')}})
  await h.hooks.mount();assert.equal(h.state.vote.value,-1);assert.equal(h.state.reason.value,'缺少依据')
  h.state.choose(1);await h.state.save();await h.state.save()
  assert.equal(attempts.length,2);assert.equal(attempts[0][3],attempts[1][3]);assert.match(h.state.status.value,/已保存/)
})
test('stopping a pending speech request prevents a late response from starting audio',async()=>{
  const speech=deferred();let created=0
  const h=await component('AnswerSpeech.vue',{messageId:'answer'},{speech:()=>speech.promise,image:async()=>new Blob(['audio'])},
    {claimPlayback(){},releasePlayback(){}},{Audio:class{constructor(){created++}},URL:{createObjectURL:()=> 'blob:test',revokeObjectURL(){}}})
  const playing=h.state.start();h.state.stop();speech.resolve({attachment:{id:'audio'},part_count:1});await playing
  assert.equal(created,0);assert.equal(h.state.active.value,false)
})
