const takeControlStyles = `
.tc-overlay {
    position: fixed;
    inset: 0;
    z-index: 2000;
    background: rgba(0, 0, 0, 0.7);
    backdrop-filter: blur(12px);
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 24px;
}

.tc-panel {
    width: 100%;
    max-width: 1080px;
    height: 90vh;
    background: #111118;
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 16px;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    box-shadow: 0 32px 96px rgba(0,0,0,0.8), 0 0 0 1px rgba(255,255,255,0.04) inset;
}

/* ── Header ── */
.tc-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 16px;
    background: #0d0d14;
    border-bottom: 1px solid rgba(255,255,255,0.06);
    flex-shrink: 0;
}

.tc-header-left {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
}

.tc-live-dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: #22c55e;
    flex-shrink: 0;
    animation: tc-pulse 2.5s ease-in-out infinite;
    box-shadow: 0 0 6px rgba(34,197,94,0.5);
}
@keyframes tc-pulse {
    0%, 100% { opacity: 1; transform: scale(1); }
    50% { opacity: 0.6; transform: scale(0.85); }
}

.tc-title {
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.04em;
    color: rgba(255,255,255,0.7);
    text-transform: uppercase;
    flex-shrink: 0;
}

.tc-divider {
    width: 1px;
    height: 14px;
    background: rgba(255,255,255,0.12);
    flex-shrink: 0;
}

.tc-status {
    font-size: 12px;
    color: rgba(255,255,255,0.4);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    animation: tc-status-flash 1.5s ease-out;
}
@keyframes tc-status-flash {
    0%   { color: #818cf8; }
    40%  { color: #818cf8; }
    100% { color: rgba(255,255,255,0.4); }
}

.tc-close-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 28px;
    height: 28px;
    background: transparent;
    border: none;
    border-radius: 6px;
    color: rgba(255,255,255,0.35);
    cursor: pointer;
    transition: background 0.12s, color 0.12s;
    flex-shrink: 0;
}
.tc-close-btn:hover {
    background: rgba(255,255,255,0.07);
    color: rgba(255,255,255,0.75);
}

/* ── Screenshot area ── */
.tc-screenshot-area {
    flex: 1;
    min-height: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
    background: #08080f;
    position: relative;
}

.tc-screenshot {
    max-width: 100%;
    max-height: 100%;
    object-fit: contain;
    user-select: none;
    -webkit-user-drag: none;
    display: block;
}

.tc-screenshot-placeholder {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 12px;
    width: 100%;
    height: 100%;
    font-size: 13px;
    color: rgba(255,255,255,0.25);
}

.tc-placeholder-spinner {
    width: 24px;
    height: 24px;
    border: 2px solid rgba(255,255,255,0.08);
    border-top-color: #6366f1;
    border-radius: 50%;
    animation: tc-spin 0.8s linear infinite;
}
@keyframes tc-spin { to { transform: rotate(360deg); } }

/* ── Toolbar (text input + quick keys) ── */
.tc-toolbar {
    padding: 10px 14px 8px;
    background: #0d0d14;
    border-top: 1px solid rgba(255,255,255,0.05);
    flex-shrink: 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
}

.tc-input-row {
    display: flex;
    gap: 6px;
}

.tc-text-input {
    flex: 1;
    padding: 7px 11px;
    font-size: 13px;
    color: rgba(255,255,255,0.85);
    background: rgba(255,255,255,0.05);
    border: 1px solid rgba(255,255,255,0.09);
    border-radius: 7px;
    outline: none;
    font-family: inherit;
    transition: border-color 0.15s;
}
.tc-text-input::placeholder { color: rgba(255,255,255,0.22); }
.tc-text-input:focus { border-color: rgba(99,102,241,0.6); background: rgba(99,102,241,0.06); }
.tc-text-input:disabled { opacity: 0.4; }

.tc-send-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 34px;
    height: 34px;
    background: #4f46e5;
    border: none;
    border-radius: 7px;
    color: #fff;
    cursor: pointer;
    flex-shrink: 0;
    transition: background 0.12s;
}
.tc-send-btn:hover { background: #4338ca; }
.tc-send-btn:disabled { opacity: 0.35; cursor: not-allowed; }

.tc-quick-keys {
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
}

.tc-quick-key {
    padding: 3px 9px;
    font-size: 11px;
    font-weight: 500;
    font-family: 'SF Mono', 'Fira Code', ui-monospace, monospace;
    color: rgba(255,255,255,0.5);
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 5px;
    cursor: pointer;
    transition: all 0.1s;
}
.tc-quick-key:hover {
    background: rgba(255,255,255,0.09);
    color: rgba(255,255,255,0.8);
    border-color: rgba(255,255,255,0.14);
}
.tc-quick-key:disabled { opacity: 0.3; cursor: not-allowed; }

/* ── Action bar (domain + save/cancel) ── */
.tc-action-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 8px 14px;
    background: #09090f;
    border-top: 1px solid rgba(255,255,255,0.06);
    flex-shrink: 0;
}

.tc-domain-row {
    display: flex;
    align-items: center;
    gap: 8px;
    flex: 1;
    min-width: 0;
}

.tc-domain-label {
    font-size: 12px;
    color: rgba(255,255,255,0.35);
    white-space: nowrap;
    flex-shrink: 0;
}

.tc-domain-input {
    flex: 1;
    min-width: 0;
    max-width: 260px;
    padding: 5px 10px;
    font-size: 12px;
    color: rgba(255,255,255,0.75);
    background: rgba(255,255,255,0.05);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 6px;
    outline: none;
    font-family: 'SF Mono', ui-monospace, monospace;
    transition: border-color 0.15s;
}
.tc-domain-input::placeholder { color: rgba(255,255,255,0.2); }
.tc-domain-input:focus { border-color: rgba(99,102,241,0.5); }

.tc-action-buttons {
    display: flex;
    gap: 6px;
    flex-shrink: 0;
}

.tc-btn-cancel {
    padding: 6px 14px;
    font-size: 12px;
    font-weight: 500;
    color: rgba(255,255,255,0.4);
    background: transparent;
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 7px;
    cursor: pointer;
    transition: all 0.12s;
}
.tc-btn-cancel:hover {
    background: rgba(255,255,255,0.06);
    color: rgba(255,255,255,0.65);
    border-color: rgba(255,255,255,0.12);
}

.tc-btn-save {
    display: flex;
    align-items: center;
    padding: 6px 16px;
    font-size: 12px;
    font-weight: 600;
    color: #fff;
    background: #4f46e5;
    border: none;
    border-radius: 7px;
    cursor: pointer;
    transition: background 0.12s, box-shadow 0.12s;
    box-shadow: 0 1px 8px rgba(79,70,229,0.35);
    white-space: nowrap;
}
.tc-btn-save:hover { background: #4338ca; box-shadow: 0 2px 12px rgba(79,70,229,0.5); }
.tc-btn-save:disabled { opacity: 0.35; cursor: not-allowed; box-shadow: none; }
`;

export default takeControlStyles;
