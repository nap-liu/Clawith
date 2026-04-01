/**
 * TakeControlPanel — Human-agent collaborative browser control panel.
 *
 * Renders a fullscreen overlay that:
 * 1. Polls screenshots from the AgentBay session every 500ms
 * 2. Forwards mouse clicks (with coordinate mapping) to the session
 * 3. Forwards keyboard input (text + special keys) to the session
 * 4. On "Complete Login", exports cookies and releases the lock
 *
 * The panel automatically acquires a Take Control lock on mount
 * and releases it on unmount/close.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { controlApi } from '../services/api';

/* ── Props ── */
interface Props {
    agentId: string;
    sessionId: string;
    onClose: () => void;
    /** Called with the last screenshot data URI when TC panel closes,
     *  so the parent live preview can update immediately. */
    onLastScreenshot?: (dataUri: string) => void;
}

/* ── Quick-key pre-defined buttons ── */
const QUICK_KEYS: { label: string; keys: string[] }[] = [
    { label: 'Tab', keys: ['Tab'] },
    { label: 'Enter', keys: ['Enter'] },
    { label: 'Esc', keys: ['Escape'] },
    { label: 'Ctrl+A', keys: ['Control', 'a'] },
    { label: 'Ctrl+C', keys: ['Control', 'c'] },
    { label: 'Ctrl+V', keys: ['Control', 'v'] },
    { label: 'Backspace', keys: ['Backspace'] },
];

/* ── Icons ── */
const CloseIcon = (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
        <path d="M4 4l8 8M12 4l-8 8" />
    </svg>
);

const SendIcon = (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M2 8h12M10 4l4 4-4 4" />
    </svg>
);

export default function TakeControlPanel({ agentId, sessionId, onClose, onLastScreenshot }: Props) {
    const [screenshot, setScreenshot] = useState<string | null>(null);
    const [textInput, setTextInput] = useState('');
    const [locked, setLocked] = useState(false);
    const [statusText, setStatusText] = useState('Acquiring control...');
    const [statusFlashKey, setStatusFlashKey] = useState(0);
    const [platformHint, setPlatformHint] = useState('');
    const imgRef = useRef<HTMLImageElement>(null);
    const pollingRef = useRef<number | null>(null);
    const mountedRef = useRef(true);
    // Track the latest screenshot data URI for passing to parent on close
    const lastScreenshotRef = useRef<string | null>(null);
    // Track the actual screen size for coordinate mapping
    // (screenshot natural dimensions may differ from screen resolution)
    const screenSizeRef = useRef<{ width: number; height: number } | null>(null);

    // Track lock state via ref for cleanup — avoids the React closure bug
    // where having `locked` in the dependency array causes the effect to
    // re-run (and re-lock) when handleCancel sets locked=false.
    const lockedRef = useRef(false);

    // Keep ref in sync with state
    useEffect(() => { lockedRef.current = locked; }, [locked]);

    // Acquire lock on mount — runs ONCE only
    useEffect(() => {
        mountedRef.current = true;
        (async () => {
            try {
                const res = await controlApi.lock(agentId, { session_id: sessionId });
                if (mountedRef.current) {
                    setLocked(true);
                    setStatusText('You are in control. Click on the screenshot to interact.');
                }
            } catch (e: any) {
                if (mountedRef.current) {
                    setStatusText(`Failed to acquire lock: ${e.message}`);
                }
            }
        })();

        return () => {
            mountedRef.current = false;
            // Release lock on unmount — uses ref to avoid stale closure
            if (lockedRef.current) {
                controlApi.unlock(agentId, {
                    session_id: sessionId,
                    export_cookies: false,
                }).catch(() => {});
            }
        };
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [agentId, sessionId]);

    // Poll screenshots using sequential setTimeout to avoid request pileup.
    // Each poll waits for the previous one to complete before scheduling the
    // next, which prevents overlapping requests that waste bandwidth and
    // introduce jitter.
    useEffect(() => {
        if (!locked) return;
        let cancelled = false;

        const poll = async () => {
            if (cancelled) return;
            try {
                const res = await controlApi.screenshot(agentId, { session_id: sessionId });
                if (!cancelled && mountedRef.current && res.screenshot) {
                    // Backend returns a complete data URI (data:image/jpeg;base64,...),
                    // use it directly without wrapping.
                    const dataUri = res.screenshot.startsWith('data:')
                        ? res.screenshot
                        : `data:image/png;base64,${res.screenshot}`;
                    setScreenshot(dataUri);
                    lastScreenshotRef.current = dataUri;
                    // Store screen size for coordinate mapping
                    if (res.screen_size) {
                        screenSizeRef.current = res.screen_size;
                    }
                }
            } catch {
                // Polling failure is non-fatal, will retry
            }
            // Schedule next poll after this one completes (sequential, not interval)
            if (!cancelled) {
                pollingRef.current = window.setTimeout(poll, 400);
            }
        };

        // Start polling immediately
        poll();

        return () => {
            cancelled = true;
            if (pollingRef.current) {
                clearTimeout(pollingRef.current);
            }
        };
    }, [locked, agentId, sessionId]);

    // Helper to update status with a visual flash — defined early for use by handlers
    const flashStatus = useCallback((text: string) => {
        setStatusText(text);
        setStatusFlashKey(k => k + 1);
    }, []);

    // Handle click on screenshot — map coordinates to actual screen resolution
    const handleScreenshotClick = useCallback(async (e: React.MouseEvent<HTMLImageElement>) => {
        if (!imgRef.current || !locked) return;

        const rect = imgRef.current.getBoundingClientRect();
        const naturalWidth = imgRef.current.naturalWidth;
        const naturalHeight = imgRef.current.naturalHeight;

        // Map display coordinates to screenshot's natural pixel coordinates
        const scaleX = naturalWidth / rect.width;
        const scaleY = naturalHeight / rect.height;
        let x = Math.round((e.clientX - rect.left) * scaleX);
        let y = Math.round((e.clientY - rect.top) * scaleY);

        // If we have the actual screen size, and it differs from the screenshot
        // dimensions (e.g., screenshot was resized during JPEG compression),
        // map from screenshot coords to screen coords.
        const ss = screenSizeRef.current;
        if (ss && (ss.width !== naturalWidth || ss.height !== naturalHeight)) {
            const mapX = ss.width / naturalWidth;
            const mapY = ss.height / naturalHeight;
            x = Math.round(x * mapX);
            y = Math.round(y * mapY);
        }

        flashStatus(`Clicking at (${x}, ${y})...`);
        try {
            const res = await controlApi.click(agentId, { session_id: sessionId, x, y });
            if (res.status === 'error') throw new Error(res.detail || 'Click failed');
            flashStatus(`Clicked at (${x}, ${y})`);
        } catch (err: any) {
            flashStatus(`Click failed: ${err.message}`);
        }
    }, [locked, agentId, sessionId, flashStatus]);


    // Handle text input
    const handleSendText = useCallback(async () => {
        if (!textInput.trim() || !locked) return;
        flashStatus(`Typing: "${textInput.slice(0, 30)}..."`);
        try {
            const res = await controlApi.type(agentId, { session_id: sessionId, text: textInput });
            if (res.status === 'error') throw new Error(res.detail || 'Type failed');
            flashStatus('Text sent');
            setTextInput('');
        } catch (err: any) {
            flashStatus(`Type failed: ${err.message}`);
        }
    }, [textInput, locked, agentId, sessionId, flashStatus]);

    // Handle quick key press
    const handleQuickKey = useCallback(async (keys: string[]) => {
        if (!locked) return;
        flashStatus(`Pressing: ${keys.join('+')}`);
        try {
            const res = await controlApi.pressKeys(agentId, { session_id: sessionId, keys });
            if (res.status === 'error') throw new Error(res.detail || 'Press failed');
            flashStatus(`Pressed: ${keys.join('+')}`);
        } catch (err: any) {
            flashStatus(`Key press failed: ${err.message}`);
        }
    }, [locked, agentId, sessionId, flashStatus]);

    // Complete login — export cookies and close
    const handleComplete = useCallback(async () => {
        if (!locked) return;
        setLocked(false);
        lockedRef.current = false;  // Prevent unmount cleanup from double-unlocking
        flashStatus('Exporting cookies...');
        try {
            // Fetch one final high-quality screenshot to hand off to the live preview
            try {
                const finalRes = await controlApi.screenshot(agentId, { session_id: sessionId });
                if (finalRes.screenshot) {
                    lastScreenshotRef.current = finalRes.screenshot.startsWith('data:') 
                        ? finalRes.screenshot 
                        : `data:image/png;base64,${finalRes.screenshot}`;
                }
            } catch (e) {
                // fallback to whatever is in lastScreenshotRef
            }
            
            const res = await controlApi.unlock(agentId, {
                session_id: sessionId,
                export_cookies: true,
                platform_hint: platformHint || undefined,
            });
            flashStatus(
                res.cookies_exported
                    ? `Login complete! ${res.cookie_count} cookies saved.`
                    : 'Session unlocked (no cookies exported).'
            );
            // Pass the last screenshot to the parent so live preview updates
            if (lastScreenshotRef.current && onLastScreenshot) {
                console.log('[TakeControl] Passing last screenshot to parent on complete, size:', lastScreenshotRef.current.length);
                onLastScreenshot(lastScreenshotRef.current);
            } else {
                console.log('[TakeControl] No screenshot to pass: ref=', !!lastScreenshotRef.current, 'callback=', !!onLastScreenshot);
            }
            setTimeout(onClose, 1200);
        } catch (err: any) {
            flashStatus(`Unlock failed: ${err.message}`);
            // Re-enable lock state if unlock request failed so user can try again
            setLocked(true);
            lockedRef.current = true;
        }
    }, [locked, agentId, sessionId, platformHint, onClose, onLastScreenshot, flashStatus]);

    // Handle cancel
    const handleCancel = useCallback(async () => {
        if (!locked) {
            // Still pass the last screenshot even if not locked
            if (lastScreenshotRef.current && onLastScreenshot) {
                console.log('[TakeControl] Passing last screenshot to parent on cancel (unlocked), size:', lastScreenshotRef.current.length);
                onLastScreenshot(lastScreenshotRef.current);
            } else {
                console.log('[TakeControl] No screenshot to pass on cancel (unlocked): ref=', !!lastScreenshotRef.current, 'callback=', !!onLastScreenshot);
            }
            onClose();
            return;
        }
        setLocked(false);
        lockedRef.current = false;  // Prevent unmount cleanup from double-unlocking
        flashStatus('Canceling...');

        // Fetch one final high-quality screenshot to hand off to the live preview
        try {
            const finalRes = await controlApi.screenshot(agentId, { session_id: sessionId });
            if (finalRes.screenshot) {
                lastScreenshotRef.current = finalRes.screenshot.startsWith('data:') 
                    ? finalRes.screenshot 
                    : `data:image/png;base64,${finalRes.screenshot}`;
            }
        } catch (e) {
            // fallback to whatever is in lastScreenshotRef
        }

        try {
            await controlApi.unlock(agentId, {
                session_id: sessionId,
                export_cookies: false,
            });
        } catch {}
        // Pass the last screenshot to the parent so live preview updates
        if (lastScreenshotRef.current && onLastScreenshot) {
            console.log('[TakeControl] Passing last screenshot to parent on cancel (locked), size:', lastScreenshotRef.current.length);
            onLastScreenshot(lastScreenshotRef.current);
        } else {
            console.log('[TakeControl] No screenshot to pass on cancel (locked): ref=', !!lastScreenshotRef.current, 'callback=', !!onLastScreenshot);
        }
        onClose();
    }, [locked, agentId, sessionId, onClose, onLastScreenshot, flashStatus]);

    return (
        <div className="tc-overlay">
            <div className="tc-panel">
                {/* Header */}
                <div className="tc-header">
                    <div className="tc-header-left">
                        <span className="tc-live-dot" />
                        <span className="tc-title">Take Control</span>
                        <span className="tc-status" key={statusFlashKey}>{statusText}</span>
                    </div>
                    <button className="tc-close-btn" onClick={handleCancel} title="Cancel">
                        {CloseIcon}
                    </button>
                </div>

                {/* Screenshot area */}
                <div className="tc-screenshot-area">
                    {screenshot ? (
                        <img
                            ref={imgRef}
                            src={screenshot}
                            alt="Browser session"
                            className="tc-screenshot"
                            onClick={handleScreenshotClick}
                            style={{ cursor: locked ? 'crosshair' : 'default' }}
                        />
                    ) : (
                        <div className="tc-screenshot-placeholder">
                            <span>Waiting for screenshot...</span>
                        </div>
                    )}
                </div>

                {/* Controls */}
                <div className="tc-controls">
                    {/* Text input */}
                    <div className="tc-text-row">
                        <input
                            className="tc-text-input"
                            type="text"
                            value={textInput}
                            onChange={(e) => setTextInput(e.target.value)}
                            onKeyDown={(e) => { if (e.key === 'Enter') handleSendText(); }}
                            placeholder="Type text to send..."
                            disabled={!locked}
                        />
                        <button
                            className="tc-send-btn"
                            onClick={handleSendText}
                            disabled={!locked || !textInput.trim()}
                        >
                            {SendIcon}
                        </button>
                    </div>

                    {/* Quick keys */}
                    <div className="tc-quick-keys">
                        {QUICK_KEYS.map((qk) => (
                            <button
                                key={qk.label}
                                className="tc-quick-key"
                                onClick={() => handleQuickKey(qk.keys)}
                                disabled={!locked}
                            >
                                {qk.label}
                            </button>
                        ))}
                    </div>

                    {/* Platform hint + action buttons */}
                    <div className="tc-action-row">
                        <input
                            className="tc-platform-input"
                            type="text"
                            value={platformHint}
                            onChange={(e) => setPlatformHint(e.target.value)}
                            placeholder="Domain to save cookies for (e.g. baidu.com)"
                        />
                        <div className="tc-action-buttons">
                            <button className="tc-btn-cancel" onClick={handleCancel}>
                                Cancel
                            </button>
                            <button
                                className="tc-btn-complete"
                                onClick={handleComplete}
                                disabled={!locked}
                            >
                                Complete Login
                            </button>
                        </div>
                    </div>
                </div>
            </div>

            <style>{takeControlStyles}</style>
        </div>
    );
}

/* ── Styles ── */
const takeControlStyles = `
.tc-overlay {
    position: fixed;
    inset: 0;
    z-index: 2000;
    background: rgba(0, 0, 0, 0.85);
    backdrop-filter: blur(8px);
    display: flex;
    align-items: center;
    justify-content: center;
}

.tc-panel {
    width: 90vw;
    max-width: 1100px;
    max-height: 92vh;
    background: var(--card-bg, #141422);
    border: 1px solid var(--border-primary, rgba(255,255,255,0.1));
    border-radius: 14px;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    box-shadow: 0 24px 80px rgba(0,0,0,0.6);
}

.tc-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 18px;
    border-bottom: 1px solid var(--border-primary, rgba(255,255,255,0.06));
    flex-shrink: 0;
}

.tc-header-left {
    display: flex;
    align-items: center;
    gap: 10px;
}

.tc-live-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: #34c759;
    animation: tc-pulse 2s ease-in-out infinite;
}
@keyframes tc-pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
}

.tc-title {
    font-size: 14px;
    font-weight: 600;
    color: var(--text-primary, #e0e0e0);
}

.tc-status {
    font-size: 12px;
    color: var(--text-tertiary, #888);
    max-width: 400px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    /* Flash animation triggers on mount (key change re-mounts the element) */
    animation: tc-status-flash 1.2s ease-out;
}
@keyframes tc-status-flash {
    0% {
        color: var(--accent, #6366f1);
        text-shadow: 0 0 8px rgba(99, 102, 241, 0.5);
    }
    30% {
        color: var(--accent, #6366f1);
        text-shadow: 0 0 4px rgba(99, 102, 241, 0.3);
    }
    100% {
        color: var(--text-tertiary, #888);
        text-shadow: none;
    }
}

.tc-close-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 32px;
    height: 32px;
    background: transparent;
    border: 1px solid transparent;
    border-radius: 6px;
    color: var(--text-tertiary, #888);
    cursor: pointer;
}
.tc-close-btn:hover {
    background: rgba(255,255,255,0.06);
    color: var(--text-primary, #e0e0e0);
}

.tc-screenshot-area {
    flex: 1;
    min-height: 300px;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
    background: #0a0a18;
}

.tc-screenshot {
    max-width: 100%;
    max-height: 100%;
    object-fit: contain;
    user-select: none;
    -webkit-user-drag: none;
}

.tc-screenshot-placeholder {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 100%;
    height: 100%;
    font-size: 13px;
    color: var(--text-tertiary, #666);
}

.tc-controls {
    padding: 12px 18px;
    border-top: 1px solid var(--border-primary, rgba(255,255,255,0.06));
    flex-shrink: 0;
    display: flex;
    flex-direction: column;
    gap: 10px;
}

.tc-text-row {
    display: flex;
    gap: 8px;
}

.tc-text-input {
    flex: 1;
    padding: 8px 12px;
    font-size: 13px;
    color: var(--text-primary, #e0e0e0);
    background: var(--bg-secondary, rgba(255,255,255,0.04));
    border: 1px solid var(--border-primary, rgba(255,255,255,0.1));
    border-radius: 6px;
    outline: none;
    font-family: inherit;
}
.tc-text-input:focus {
    border-color: var(--accent, #6366f1);
}

.tc-send-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 36px;
    height: 36px;
    background: var(--accent, #6366f1);
    border: none;
    border-radius: 6px;
    color: #fff;
    cursor: pointer;
    flex-shrink: 0;
}
.tc-send-btn:hover { opacity: 0.9; }
.tc-send-btn:disabled { opacity: 0.4; cursor: not-allowed; }

.tc-quick-keys {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
}

.tc-quick-key {
    padding: 4px 10px;
    font-size: 11px;
    font-weight: 500;
    font-family: 'SF Mono', 'Fira Code', monospace;
    color: var(--text-secondary, #b0b0b0);
    background: var(--bg-tertiary, rgba(255,255,255,0.05));
    border: 1px solid var(--border-primary, rgba(255,255,255,0.08));
    border-radius: 4px;
    cursor: pointer;
    transition: all 0.12s;
}
.tc-quick-key:hover {
    background: rgba(255,255,255,0.1);
    border-color: rgba(255,255,255,0.15);
    color: var(--text-primary, #e0e0e0);
}
.tc-quick-key:disabled { opacity: 0.4; cursor: not-allowed; }

.tc-action-row {
    display: flex;
    align-items: center;
    gap: 10px;
}

.tc-platform-input {
    flex: 1;
    padding: 7px 10px;
    font-size: 12px;
    color: var(--text-primary, #e0e0e0);
    background: var(--bg-secondary, rgba(255,255,255,0.04));
    border: 1px solid var(--border-primary, rgba(255,255,255,0.08));
    border-radius: 6px;
    outline: none;
}
.tc-platform-input:focus {
    border-color: var(--accent, #6366f1);
}

.tc-action-buttons {
    display: flex;
    gap: 8px;
    flex-shrink: 0;
}

.tc-btn-cancel {
    padding: 7px 16px;
    font-size: 13px;
    color: var(--text-secondary, #b0b0b0);
    background: transparent;
    border: 1px solid var(--border-primary, rgba(255,255,255,0.1));
    border-radius: 6px;
    cursor: pointer;
}
.tc-btn-cancel:hover {
    background: rgba(255,255,255,0.06);
}

.tc-btn-complete {
    padding: 7px 20px;
    font-size: 13px;
    font-weight: 600;
    color: #fff;
    background: #34c759;
    border: none;
    border-radius: 6px;
    cursor: pointer;
    transition: opacity 0.15s;
}
.tc-btn-complete:hover { opacity: 0.9; }
.tc-btn-complete:disabled { opacity: 0.4; cursor: not-allowed; }
`;
