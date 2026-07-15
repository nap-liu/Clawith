import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { speechApi } from '../services/api';

export type SpeechInputStatus = 'idle' | 'connecting' | 'recording' | 'stopping' | 'error';

type SpeechCallbacks = {
    onInterim: (text: string) => void;
    onFinal: (text: string) => void;
    onCancel: () => void;
};

type SpeechResources = {
    stream: MediaStream;
    context: AudioContext;
    source: MediaStreamAudioSourceNode;
    worklet: AudioWorkletNode;
    sink: MediaStreamAudioDestinationNode;
    socket: WebSocket;
    timer: number | null;
    captureConnected: boolean;
    closedIntentionally: boolean;
};

function stopMediaStream(stream: MediaStream) {
    stream.getTracks().forEach((track) => track.stop());
}

export function insertSpeechTranscript(base: string, transcript: string, selectionStart: number, selectionEnd: number) {
    const spoken = transcript.trim();
    const start = Math.max(0, Math.min(selectionStart, base.length));
    const end = Math.max(start, Math.min(selectionEnd, base.length));
    if (!spoken) return { value: base, caret: start };

    const before = base.slice(0, start);
    const after = base.slice(end);
    const leadingSpace = /[A-Za-z0-9]$/.test(before) && /^[A-Za-z0-9]/.test(spoken) ? ' ' : '';
    const trailingSpace = /[A-Za-z0-9]$/.test(spoken) && /^[A-Za-z0-9]/.test(after) ? ' ' : '';
    const inserted = `${leadingSpace}${spoken}${trailingSpace}`;
    return {
        value: `${before}${inserted}${after}`,
        caret: before.length + inserted.length,
    };
}

export function useSpeechInput(callbacks: SpeechCallbacks) {
    const [status, setStatus] = useState<SpeechInputStatus>('idle');
    const [error, setError] = useState('');
    const [transcript, setTranscript] = useState('');
    const resourcesRef = useRef<SpeechResources | null>(null);
    const callbacksRef = useRef(callbacks);
    const mountedRef = useRef(true);
    const statusRef = useRef<SpeechInputStatus>('idle');

    useEffect(() => {
        callbacksRef.current = callbacks;
    }, [callbacks]);

    useEffect(() => {
        statusRef.current = status;
    }, [status]);

    const supported = useMemo(() => {
        if (typeof window === 'undefined' || typeof navigator === 'undefined') return false;
        const AudioContextCtor = window.AudioContext || (window as any).webkitAudioContext;
        return !!navigator.mediaDevices?.getUserMedia
            && !!AudioContextCtor
            && typeof window.AudioWorkletNode !== 'undefined'
            && typeof window.WebSocket !== 'undefined';
    }, []);

    const cleanup = useCallback((closeSocket = true) => {
        const resources = resourcesRef.current;
        resourcesRef.current = null;
        if (!resources) return;
        resources.closedIntentionally = true;
        if (resources.timer !== null) window.clearTimeout(resources.timer);
        if (resources.captureConnected) {
            try { resources.source.disconnect(); } catch { /* already disconnected */ }
            try { resources.worklet.disconnect(); } catch { /* already disconnected */ }
            try { resources.sink.disconnect(); } catch { /* already disconnected */ }
        }
        stopMediaStream(resources.sink.stream);
        stopMediaStream(resources.stream);
        void resources.context.close().catch(() => undefined);
        if (closeSocket && resources.socket.readyState < WebSocket.CLOSING) resources.socket.close();
    }, []);

    const fail = useCallback((message: string) => {
        cleanup();
        if (!mountedRef.current) return;
        setError(message);
        setStatus('error');
    }, [cleanup]);

    const cancel = useCallback(() => {
        const resources = resourcesRef.current;
        if (resources?.socket.readyState === WebSocket.OPEN) {
            resources.socket.send(JSON.stringify({ type: 'cancel' }));
        }
        cleanup();
        setTranscript('');
        setError('');
        setStatus('idle');
        callbacksRef.current.onCancel();
    }, [cleanup]);

    const stop = useCallback(() => {
        const resources = resourcesRef.current;
        if (!resources) return;
        if (statusRef.current === 'connecting') {
            cancel();
            return;
        }
        if (statusRef.current !== 'recording') return;
        setStatus('stopping');
        if (resources.timer !== null) {
            window.clearTimeout(resources.timer);
            resources.timer = null;
        }
        if (resources.captureConnected) {
            resources.source.disconnect();
            resources.worklet.disconnect();
            resources.sink.disconnect();
            resources.captureConnected = false;
        }
        stopMediaStream(resources.sink.stream);
        stopMediaStream(resources.stream);
        void resources.context.close().catch(() => undefined);
        if (resources.socket.readyState === WebSocket.OPEN) {
            resources.socket.send(JSON.stringify({ type: 'stop' }));
        } else {
            fail('语音连接已断开');
        }
    }, [cancel, fail]);

    const start = useCallback(async () => {
        if (!supported || (status !== 'idle' && status !== 'error')) return;
        setError('');
        setTranscript('');
        setStatus('connecting');

        const AudioContextCtor = window.AudioContext || (window as any).webkitAudioContext;
        let context: AudioContext | null = null;
        let stream: MediaStream | null = null;
        try {
            const [nextStream, ticket] = await Promise.all([
                navigator.mediaDevices.getUserMedia({
                    audio: {
                        channelCount: { ideal: 1 },
                        sampleRate: { ideal: 16000 },
                        echoCancellation: true,
                        noiseSuppression: true,
                        autoGainControl: true,
                    },
                    video: false,
                }),
                speechApi.createTicket(),
            ]);
            stream = nextStream;
            const audioContext: AudioContext = new AudioContextCtor({ latencyHint: 'interactive' });
            context = audioContext;
            await audioContext.audioWorklet.addModule('/audio/pcm16-worklet.js');

            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            const socket = new WebSocket(`${protocol}//${window.location.host}/ws/speech`);
            const source = audioContext.createMediaStreamSource(stream);
            const worklet = new AudioWorkletNode(audioContext, 'clawith-pcm16', {
                numberOfInputs: 1,
                numberOfOutputs: 1,
                outputChannelCount: [1],
            });
            // Keep the worklet graph active without touching the physical speaker.
            // Some WebViews fail AudioContext initialization when destination starts
            // an output renderer even though this feature only needs microphone input.
            const sink = audioContext.createMediaStreamDestination();
            const resources: SpeechResources = {
                stream,
                context: audioContext,
                source,
                worklet,
                sink,
                socket,
                timer: null,
                captureConnected: false,
                closedIntentionally: false,
            };
            resourcesRef.current = resources;

            worklet.port.onmessage = (event: MessageEvent<ArrayBuffer>) => {
                if (resources.socket.readyState === WebSocket.OPEN && resources.captureConnected) {
                    resources.socket.send(event.data);
                }
            };

            socket.onopen = () => {
                socket.send(JSON.stringify({ type: 'authenticate', ticket: ticket.ticket }));
            };
            socket.onmessage = (event) => {
                const message = JSON.parse(String(event.data));
                if (message.type === 'ready') {
                    source.connect(worklet);
                    worklet.connect(sink);
                    resources.captureConnected = true;
                    void audioContext.resume().then(() => {
                        if (resourcesRef.current !== resources) return;
                        resources.timer = window.setTimeout(() => stop(), Number(message.max_duration || 60) * 1000);
                        setStatus('recording');
                    }).catch(() => fail('无法启动麦克风音频处理，请关闭其他录音应用后重试'));
                    return;
                }
                if (message.type === 'partial' || message.type === 'final') {
                    const text = String(message.text || '');
                    setTranscript(text);
                    callbacksRef.current.onInterim(text);
                    return;
                }
                if (message.type === 'completed') {
                    const text = String(message.text || '').trim();
                    cleanup();
                    setStatus('idle');
                    setTranscript('');
                    if (text) callbacksRef.current.onFinal(text);
                    return;
                }
                if (message.type === 'error') {
                    fail(String(message.message || '语音识别失败'));
                }
            };
            socket.onerror = () => fail('无法连接语音识别服务');
            socket.onclose = () => {
                if (!resources.closedIntentionally && resourcesRef.current === resources) {
                    fail('语音识别连接已关闭');
                }
            };
        } catch (reason: any) {
            if (stream) stopMediaStream(stream);
            if (context) void context.close().catch(() => undefined);
            const message = reason?.name === 'NotAllowedError'
                ? '请允许使用麦克风后重试'
                : reason?.message || '无法启动语音输入';
            fail(message);
        }
    }, [cleanup, fail, status, stop, supported]);

    useEffect(() => {
        mountedRef.current = true;
        return () => {
            mountedRef.current = false;
            cleanup();
        };
    }, [cleanup]);

    return {
        status,
        error,
        transcript,
        supported,
        isActive: status === 'connecting' || status === 'recording' || status === 'stopping',
        start,
        stop,
        cancel,
    };
}
