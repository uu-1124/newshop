export function encodeRecording(samples: Float32Array, sampleRate: number): Blob {
  if (
    sampleRate < 8000 ||
    sampleRate > 96000 ||
    !samples.length ||
    samples.length > sampleRate * 60
  )
    throw new Error('录音须在60秒以内')
  const rate = 16000
  const count = Math.floor((samples.length * rate) / sampleRate)
  if (!count) throw new Error('录音时间太短，请重录')
  const bytes = new ArrayBuffer(44 + count * 2),
    view = new DataView(bytes)
  const text = (at: number, value: string) => {
    for (let i = 0; i < value.length; i++) view.setUint8(at + i, value.charCodeAt(i))
  }
  text(0, 'RIFF')
  view.setUint32(4, bytes.byteLength - 8, true)
  text(8, 'WAVE')
  text(12, 'fmt ')
  view.setUint32(16, 16, true)
  view.setUint16(20, 1, true)
  view.setUint16(22, 1, true)
  view.setUint32(24, rate, true)
  view.setUint32(28, rate * 2, true)
  view.setUint16(32, 2, true)
  view.setUint16(34, 16, true)
  text(36, 'data')
  view.setUint32(40, count * 2, true)
  for (let i = 0; i < count; i++) {
    const position = (i * sampleRate) / rate,
      start = Math.floor(position),
      fraction = position - start
    const sample = Math.max(
      -1,
      Math.min(
        1,
        samples[start] * (1 - fraction) +
          samples[Math.min(start + 1, samples.length - 1)] * fraction,
      ),
    )
    view.setInt16(44 + i * 2, sample < 0 ? sample * 32768 : sample * 32767, true)
  }
  return new Blob([bytes], { type: 'audio/wav' })
}

export type Recording = { finish(): Promise<Blob>; cancel(): Promise<void> }
export async function startRecording(onLimit: () => void): Promise<Recording> {
  if (
    !globalThis.isSecureContext ||
    !navigator.mediaDevices?.getUserMedia ||
    !globalThis.AudioContext
  )
    throw new Error('当前浏览器不能录音，请使用HTTPS或本机地址，或直接输入文字')
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1 },
    video: false,
  })
  let context: AudioContext | undefined
  let node: AudioWorkletNode | undefined
  let source: MediaStreamAudioSourceNode | undefined
  let url = ''
  let frames: Float32Array[] = [],
    length = 0,
    closed = false
  let timer: ReturnType<typeof setTimeout> | undefined
  let flushed: (() => void) | undefined
  const cleanup = async () => {
    if (closed) return
    closed = true
    clearTimeout(timer)
    node?.disconnect()
    source?.disconnect()
    if (node) node.port.onmessage = null
    stream.getTracks().forEach((track) => track.stop())
    if (context && context.state !== 'closed') await context.close()
    if (url) URL.revokeObjectURL(url)
  }
  try {
    context = new AudioContext({ sampleRate: 16000 })
    await context.resume()
    url = URL.createObjectURL(
      new Blob(
        [
          `
      class Capture extends AudioWorkletProcessor {
        constructor() {
          super(); this.buffer = new Float32Array(2048); this.offset = 0;
          this.port.onmessage = () => {
            if (this.offset) this.port.postMessage(this.buffer.slice(0, this.offset));
            this.offset = 0; this.port.postMessage('flushed');
          };
        }
        process(inputs, outputs) {
          const data = inputs[0]?.[0];
          if (data) for (const sample of data) {
            this.buffer[this.offset++] = sample;
            if (this.offset === this.buffer.length) { this.port.postMessage(this.buffer); this.buffer = new Float32Array(2048); this.offset = 0; }
          }
          return true;
        }
      }
      registerProcessor('member-recording', Capture);
    `,
        ],
        { type: 'text/javascript' },
      ),
    )
    await context.audioWorklet.addModule(url)
    source = context.createMediaStreamSource(stream)
    node = new AudioWorkletNode(context, 'member-recording')
    node.port.onmessage = (event) => {
      if (closed) return
      if (event.data === 'flushed') {
        flushed?.()
        return
      }
      const data = event.data as Float32Array
      const remaining = Math.max(0, context!.sampleRate * 60 - length)
      if (remaining) {
        const block = data.slice(0, remaining)
        frames.push(block)
        length += block.length
      }
    }
    source.connect(node)
    // Worklet output is silence; connect it so the browser continues processing.
    node.connect(context.destination)
    timer = setTimeout(onLimit, 60000)
    return {
      async finish() {
        const rate = context!.sampleRate
        source?.disconnect()
        await new Promise<void>((resolve) => {
          const timeout = setTimeout(resolve, 200)
          flushed = () => {
            clearTimeout(timeout)
            resolve()
          }
          node?.port.postMessage('flush')
        })
        await cleanup()
        const all = new Float32Array(length)
        let offset = 0
        for (const block of frames) {
          all.set(block, offset)
          offset += block.length
        }
        frames = []
        return encodeRecording(all, rate)
      },
      async cancel() {
        frames = []
        await cleanup()
      },
    }
  } catch (error) {
    await cleanup()
    throw error
  }
}

let stopPlayback: (() => void) | undefined
export function claimPlayback(stop: () => void) {
  stopPlayback?.()
  stopPlayback = stop
}
export function releasePlayback(stop: () => void) {
  if (stopPlayback === stop) stopPlayback = undefined
}
export function stopAllSpeech() {
  stopPlayback?.()
  stopPlayback = undefined
}
