import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'
const source = await readFile(new URL('../src/services/agentAudio.ts', import.meta.url), 'utf8')
const javascript = ts.transpileModule(source, {compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022}}).outputText
const { encodeRecording, claimPlayback, releasePlayback, stopAllSpeech } = await import('data:text/javascript;base64,'+Buffer.from(javascript).toString('base64'))
test('recording produces bounded mono PCM WAV and resamples to 16kHz',async()=>{
  const samples = new Float32Array(48000).fill(0.5)
  const wav=await encodeRecording(samples,48000).arrayBuffer(),v=new DataView(wav)
  assert.equal(wav.byteLength,32044);assert.equal(v.getUint32(24,true),16000)
  assert.equal(v.getUint16(22,true),1);assert.equal(v.getInt16(44,true),16383)
  assert.throws(()=>encodeRecording(new Float32Array(),16000))
  assert.throws(()=>encodeRecording(new Float32Array(16000*61),16000))
})
test('starting another answer stops prior playback and releasing old player cannot stop new one',()=>{
  let a=0,b=0
  const left=()=>{a++},right=()=>{b++}
  claimPlayback(left);claimPlayback(right);assert.equal(a,1)
  releasePlayback(left);stopAllSpeech();assert.equal(b,1)
  stopAllSpeech();assert.equal(b,1)
})
