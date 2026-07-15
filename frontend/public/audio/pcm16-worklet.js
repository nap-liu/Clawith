class ClawithPcm16Processor extends AudioWorkletProcessor {
    constructor() {
        super();
        this.targetRate = 16000;
        this.phase = 0;
        this.sum = 0;
        this.count = 0;
        this.chunk = new Int16Array(2048);
        this.chunkOffset = 0;
    }

    pushSample(value) {
        const clipped = Math.max(-1, Math.min(1, value));
        this.chunk[this.chunkOffset] = clipped < 0 ? Math.round(clipped * 32768) : Math.round(clipped * 32767);
        this.chunkOffset += 1;
        if (this.chunkOffset === this.chunk.length) {
            const buffer = this.chunk.buffer;
            this.port.postMessage(buffer, [buffer]);
            this.chunk = new Int16Array(2048);
            this.chunkOffset = 0;
        }
    }

    process(inputs) {
        const channel = inputs[0]?.[0];
        if (!channel) return true;

        for (let index = 0; index < channel.length; index += 1) {
            this.sum += channel[index];
            this.count += 1;
            this.phase += this.targetRate;
            if (this.phase >= sampleRate) {
                this.pushSample(this.sum / this.count);
                this.phase -= sampleRate;
                this.sum = 0;
                this.count = 0;
            }
        }
        return true;
    }
}

registerProcessor('clawith-pcm16', ClawithPcm16Processor);
