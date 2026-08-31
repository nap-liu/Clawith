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
import { useTranslation } from 'react-i18next';
import { controlApi } from '../services/api';
import takeControlStyles from './takeControlStyles';

/* ── Props ── */
interface Props {
    agentId: string;
    sessionId: string;
    onClose: () => void;
    /** Which AgentBay environment to connect to: 'browser' | 'computer' | 'code'. Default: 'browser'. */
    envType?: 'browser' | 'computer' | 'code';
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
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
        <path d="M4 4l8 8M12 4l-8 8" />
    </svg>
);

const SendIcon = (
    <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M2 8h12M10 4l4 4-4 4" />
    </svg>
);

const SaveIcon = (
    <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" style={{ display: 'inline', verticalAlign: 'middle', marginRight: 5 }}>
        <path d="M13 2H3a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h10a1 1 0 0 0 1-1V5l-3-3z" />
        <polyline points="9 2 9 6 4 6" />
        <path d="M4 10h8" />
    </svg>
);

export default function TakeControlPanel({ agentId, sessionId, onClose, envType = 'browser', onLastScreenshot }: Props) {
    const { t } = useTranslation();
    const [screenshot, setScreenshot] = useState<string | null>(null);
    const [textInput, setTextInput] = useState('');
    const [locked, setLocked] = useState(false);
    const [statusText, setStatusText] = useState(t('takeControl.acquiringControl'));
    const [statusFlashKey, setStatusFlashKey] = useState(0);
    // Domain auto-populated from current page URL; user can still edit
    const [platformHint, setPlatformHint] = useState('');
    const imgRef = useRef<HTMLImageElement>(null);
    const pollingRef = useRef<number | null>(null);
    const mountedRef = useRef(true);
    // Track the latest screenshot data URI for passing to parent on close
    const lastScreenshotRef = useRef<string | null>(null);
    // Track the actual screen size for coordinate mapping
    const screenSizeRef = useRef<{ width: number; height: number } | null>(null);

    // Drag gesture state
    const dragOriginRef = useRef<{ x: number; y: number; screenX: number; screenY: number } | null>(null);
    const [dragEnd, setDragEnd] = useState<{ x: number; y: number } | null>(null);
    const isDraggingRef = useRef(false);

    // Track lock state via ref for cleanup
    const lockedRef = useRef(false);
    useEffect(() => { lockedRef.current = locked; }, [locked]);

    // Acquire lock on mount, then auto-fetch the current page URL to pre-fill domain
    useEffect(() => {
        mountedRef.current = true;
        (async () => {
            try {
                const res = await controlApi.lock(agentId, { session_id: sessionId, env_type: envType });
                if (mountedRef.current) {
                    setLocked(true);
                    setStatusText(t('takeControl.inControl'));

                    // Auto-populate domain from the current active page URL
                    try {
                        const urlRes = await controlApi.currentUrl(agentId, { session_id: sessionId });
                        if (urlRes.url) {
                            const hostname = new URL(urlRes.url).hostname.replace(/^www\./, '');
                            if (hostname && hostname !== 'about:blank' && hostname !== '') {
                                setPlatformHint(hostname);
                            }
                        }
                    } catch {
                        // Non-fatal — user can type it manually
                    }
                }
            } catch (e: any) {
                if (mountedRef.current) {
                    setStatusText(t('takeControl.lockFailed', { message: e.message }));
                }
            }
        })();

        return () => {
            mountedRef.current = false;
            if (lockedRef.current) {
                controlApi.unlock(agentId, { session_id: sessionId, export_cookies: false }).catch(() => {});
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

    // Map display (img element) coordinates to actual screen pixel coordinates
    const mapToScreenCoords = useCallback((clientX: number, clientY: number) => {
        if (!imgRef.current) return { x: 0, y: 0 };
        const rect = imgRef.current.getBoundingClientRect();
        const naturalWidth = imgRef.current.naturalWidth;
        const naturalHeight = imgRef.current.naturalHeight;
        const scaleX = naturalWidth / rect.width;
        const scaleY = naturalHeight / rect.height;
        let x = Math.round((clientX - rect.left) * scaleX);
        let y = Math.round((clientY - rect.top) * scaleY);
        // Map from screenshot pixel coords to actual screen resolution if they differ
        const ss = screenSizeRef.current;
        if (ss && (ss.width !== naturalWidth || ss.height !== naturalHeight)) {
            x = Math.round(x * (ss.width / naturalWidth));
            y = Math.round(y * (ss.height / naturalHeight));
        }
        return { x, y };
    }, []);

    // --- Mouse event handlers on screenshot ---

    // mousedown: begin a potential drag gesture
    const handleMouseDown = useCallback((e: React.MouseEvent<HTMLImageElement>) => {
        if (!locked || !imgRef.current) return;
        e.preventDefault();
        const coords = mapToScreenCoords(e.clientX, e.clientY);
        dragOriginRef.current = { x: coords.x, y: coords.y, screenX: e.clientX, screenY: e.clientY };
        isDraggingRef.current = false;
        setDragEnd(null);
    }, [locked, mapToScreenCoords]);

    // mousemove: update drag overlay once the gesture exceeds a 5px threshold
    const handleMouseMove = useCallback((e: React.MouseEvent<HTMLImageElement>) => {
        if (!locked || !imgRef.current || !dragOriginRef.current) return;
        const dx = e.clientX - dragOriginRef.current.screenX;
        const dy = e.clientY - dragOriginRef.current.screenY;
        if (!isDraggingRef.current && Math.hypot(dx, dy) > 5) {
            isDraggingRef.current = true;
            flashStatus(t('takeControl.dragToRelease'));
        }
        if (isDraggingRef.current) {
            setDragEnd({ x: e.clientX, y: e.clientY });
        }
    }, [locked, flashStatus]);

    // mouseup: commit drag or fall back to a click
    const handleMouseUp = useCallback(async (e: React.MouseEvent<HTMLImageElement>) => {
        if (!locked || !imgRef.current || !dragOriginRef.current) return;
        const origin = dragOriginRef.current;
        dragOriginRef.current = null;
        setDragEnd(null);

        if (isDraggingRef.current) {
            // --- DRAG ---
            isDraggingRef.current = false;
            const to = mapToScreenCoords(e.clientX, e.clientY);
            flashStatus(t('takeControl.dragging', { fromX: origin.x, fromY: origin.y, toX: to.x, toY: to.y }));
            try {
                const res = await controlApi.drag(agentId, {
                    session_id: sessionId,
                    from_x: origin.x,
                    from_y: origin.y,
                    to_x: to.x,
                    to_y: to.y,
                });
                if (res.status === 'error') throw new Error(res.detail || 'Drag failed');
                flashStatus(t('takeControl.dragComplete'));
            } catch (err: any) {
                flashStatus(t('takeControl.dragFailed', { message: err.message }));
            }
        } else {
            // --- CLICK (no significant movement) ---
            isDraggingRef.current = false;
            const coords = mapToScreenCoords(e.clientX, e.clientY);
            flashStatus(t('takeControl.clickingAt', { x: coords.x, y: coords.y }));
            try {
                const res = await controlApi.click(agentId, { session_id: sessionId, x: coords.x, y: coords.y });
                if (res.status === 'error') throw new Error(res.detail || 'Click failed');
                flashStatus(t('takeControl.clickedAt', { x: coords.x, y: coords.y }));
            } catch (err: any) {
                flashStatus(t('takeControl.clickFailed', { message: err.message }));
            }
        }
    }, [locked, agentId, sessionId, mapToScreenCoords, flashStatus]);

    // Cancel drag if mouse leaves the screenshot area
    const handleMouseLeave = useCallback(() => {
        if (isDraggingRef.current) {
            isDraggingRef.current = false;
            dragOriginRef.current = null;
            setDragEnd(null);
            flashStatus(t('takeControl.inControl'));
        }
    }, [flashStatus]);


    // Handle text input
    const handleSendText = useCallback(async () => {
        if (!textInput.trim() || !locked) return;
        flashStatus(t('takeControl.typing', { text: textInput.slice(0, 30) }));
        try {
            const res = await controlApi.type(agentId, { session_id: sessionId, text: textInput });
            if (res.status === 'error') throw new Error(res.detail || 'Type failed');
            flashStatus(t('takeControl.textSent'));
            setTextInput('');
        } catch (err: any) {
            flashStatus(t('takeControl.typeFailed', { message: err.message }));
        }
    }, [textInput, locked, agentId, sessionId, flashStatus]);

    // Handle quick key press
    const handleQuickKey = useCallback(async (keys: string[]) => {
        if (!locked) return;
        flashStatus(t('takeControl.pressing', { keys: keys.join('+') }));
        try {
            const res = await controlApi.pressKeys(agentId, { session_id: sessionId, keys });
            if (res.status === 'error') throw new Error(res.detail || 'Press failed');
            flashStatus(t('takeControl.pressed', { keys: keys.join('+') }));
        } catch (err: any) {
            flashStatus(t('takeControl.pressFailed', { message: err.message }));
        }
    }, [locked, agentId, sessionId, flashStatus]);

    // Complete login — export cookies and close
    const handleComplete = useCallback(async () => {
        if (!locked) return;
        setLocked(false);
        lockedRef.current = false;  // Prevent unmount cleanup from double-unlocking
        flashStatus(t('takeControl.exportingCookies'));
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
                    ? t('takeControl.loginComplete', { count: res.cookie_count })
                    : t('takeControl.sessionUnlocked')
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
            flashStatus(t('takeControl.unlockFailed', { message: err.message }));
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
        flashStatus(t('takeControl.canceling'));

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

                {/* ── Header ── */}
                <div className="tc-header">
                    <div className="tc-header-left">
                        <span className="tc-live-dot" />
                        <span className="tc-title">{t('takeControl.title')}</span>
                        <span className="tc-divider" />
                        <span className="tc-status" key={statusFlashKey}>{statusText}</span>
                    </div>
                    <button className="tc-close-btn" onClick={handleCancel} title={t('takeControl.exitWithoutSaving')}>
                        {CloseIcon}
                    </button>
                </div>

                {/* ── Screenshot area ── */}
                <div className="tc-screenshot-area">
                    {screenshot ? (
                        <div style={{ position: 'relative', lineHeight: 0, width: '100%', height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                            <img
                                ref={imgRef}
                                src={screenshot}
                                alt={t('takeControl.browserSession')}
                                className="tc-screenshot"
                                onMouseDown={handleMouseDown}
                                onMouseMove={handleMouseMove}
                                onMouseUp={handleMouseUp}
                                onMouseLeave={handleMouseLeave}
                                style={{ cursor: locked ? 'crosshair' : 'default', userSelect: 'none', display: 'block', maxWidth: '100%', maxHeight: '100%', objectFit: 'contain' }}
                                draggable={false}
                            />
                            {/* Drag arrow overlay */}
                            {dragEnd && dragOriginRef.current && (
                                <svg
                                    style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', pointerEvents: 'none' }}
                                    viewBox={`0 0 ${imgRef.current?.offsetWidth ?? 800} ${imgRef.current?.offsetHeight ?? 600}`}
                                    preserveAspectRatio="none"
                                >
                                    <defs>
                                        <marker id="tc-arrowhead" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto">
                                            <polygon points="0 0, 8 3, 0 6" fill="#6366f1" />
                                        </marker>
                                    </defs>
                                    <circle
                                        cx={dragOriginRef.current.screenX - (imgRef.current?.getBoundingClientRect().left ?? 0)}
                                        cy={dragOriginRef.current.screenY - (imgRef.current?.getBoundingClientRect().top ?? 0)}
                                        r="5" fill="#6366f1" opacity="0.9"
                                    />
                                    <line
                                        x1={dragOriginRef.current.screenX - (imgRef.current?.getBoundingClientRect().left ?? 0)}
                                        y1={dragOriginRef.current.screenY - (imgRef.current?.getBoundingClientRect().top ?? 0)}
                                        x2={dragEnd.x - (imgRef.current?.getBoundingClientRect().left ?? 0)}
                                        y2={dragEnd.y - (imgRef.current?.getBoundingClientRect().top ?? 0)}
                                        stroke="#6366f1" strokeWidth="2" strokeDasharray="5 3"
                                        markerEnd="url(#tc-arrowhead)" opacity="0.9"
                                    />
                                </svg>
                            )}
                        </div>
                    ) : (
                        <div className="tc-screenshot-placeholder">
                            <div className="tc-placeholder-spinner" />
                            <span>{t('takeControl.connecting')}</span>
                        </div>
                    )}
                </div>

                {/* ── Toolbar ── */}
                <div className="tc-toolbar">
                    {/* Input + Send */}
                    <div className="tc-input-row">
                        <input
                            className="tc-text-input"
                            type="text"
                            value={textInput}
                            onChange={(e) => setTextInput(e.target.value)}
                            onKeyDown={(e) => { if (e.key === 'Enter') handleSendText(); }}
                            placeholder={t('takeControl.typeToSend')}
                            disabled={!locked}
                        />
                        <button
                            className="tc-send-btn"
                            onClick={handleSendText}
                            disabled={!locked || !textInput.trim()}
                            title={t('takeControl.sendText')}
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
                </div>

                {/* ── Action bar ── */}
                <div className="tc-action-bar">
                    <div className="tc-domain-row">
                        <span className="tc-domain-label">{t('takeControl.saveLoginStateTo')}</span>
                        <input
                            className="tc-domain-input"
                            type="text"
                            value={platformHint}
                            onChange={(e) => setPlatformHint(e.target.value)}
                            placeholder="example.com"
                        />
                    </div>
                    <div className="tc-action-buttons">
                        <button className="tc-btn-cancel" onClick={handleCancel}>
                            {t('takeControl.exit')}
                        </button>
                        <button
                            className="tc-btn-save"
                            onClick={handleComplete}
                            disabled={!locked}
                        >
                            {SaveIcon} {t('takeControl.saveLoginState')}
                        </button>
                    </div>
                </div>

            </div>

            <style>{takeControlStyles}</style>
        </div>
    );
}
