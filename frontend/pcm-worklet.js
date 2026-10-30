class PCMWorklet extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const opts = options.processorOptions || {};
    this.targetRate = opts.targetRate || 16000;
    this.ratio = sampleRate / this.targetRate;
    this.chunkSamples = opts.chunkSamples || 1600;
    this.pos = 0;
    this.buf = new Float32Array(0);
    this.chunk = new Int16Array(this.chunkSamples);
    this.chunkLen = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input.length) return true;
    const channel = input[0];
    if (!channel || !channel.length) return true;

    if (this.buf.length) {
      const merged = new Float32Array(this.buf.length + channel.length);
      merged.set(this.buf, 0);
      merged.set(channel, this.buf.length);
      this.buf = merged;
    } else {
      this.buf = new Float32Array(channel);
    }

    while (this.pos < this.buf.length - 1) {
      const index = Math.floor(this.pos);
      const frac = this.pos - index;
      let sample = this.buf[index] * (1 - frac) + this.buf[index + 1] * frac;
      sample = sample < -1 ? -1 : sample > 1 ? 1 : sample;
      this.chunk[this.chunkLen++] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;

      if (this.chunkLen === this.chunk.length) {
        this.port.postMessage(this.chunk.buffer, [this.chunk.buffer]);
        this.chunk = new Int16Array(this.chunkSamples);
        this.chunkLen = 0;
      }
      this.pos += this.ratio;
    }

    const consumed = Math.floor(this.pos);
    if (consumed > 0) {
      this.buf = this.buf.slice(consumed);
      this.pos -= consumed;
    }
    return true;
  }
}

registerProcessor("pcm-worklet", PCMWorklet);
