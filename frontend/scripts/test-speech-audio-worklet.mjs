import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../public/audio/pcm16-worklet.js', import.meta.url), 'utf8');
let Processor;
const posted = [];

class AudioWorkletProcessor {
    constructor() {
        this.port = {
            postMessage(buffer) {
                posted.push(new Int16Array(buffer.slice(0)));
            },
        };
    }
}

vm.runInNewContext(source, {
    AudioWorkletProcessor,
    sampleRate: 48000,
    registerProcessor(name, value) {
        assert.equal(name, 'clawith-pcm16');
        Processor = value;
    },
    Int16Array,
    Math,
});

assert.ok(Processor, 'worklet must register the clawith-pcm16 processor');
const processor = new Processor();
const block = new Float32Array(128).fill(0.5);
for (let index = 0; index < 48; index += 1) processor.process([[block]]);

assert.equal(posted.length, 1, '6144 samples at 48kHz should emit one 2048-sample 16kHz chunk');
assert.equal(posted[0].length, 2048);
assert.ok(posted[0].every((sample) => sample >= 16383 && sample <= 16384));

console.log('speech audio worklet tests passed');
