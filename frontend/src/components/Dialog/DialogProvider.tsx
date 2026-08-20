import { createContext, useCallback, useContext, useEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { IconAlertTriangle, IconCheck, IconInfoCircle, IconX } from '@tabler/icons-react';
import Button from '../ui/Button';
import './DialogProvider.css';

type DialogType = 'info' | 'success' | 'warning' | 'error';

interface AlertOptions {
    title?: string;
    type?: DialogType;
    details?: string;
    confirmLabel?: string;
}

interface ConfirmOptions {
    title?: string;
    danger?: boolean;
    confirmLabel?: string;
    cancelLabel?: string;
}

interface DialogContextValue {
    alert: (message: string, options?: AlertOptions) => Promise<void>;
    confirm: (message: string, options?: ConfirmOptions) => Promise<boolean>;
}

const DialogContext = createContext<DialogContextValue | null>(null);

type ModalState =
    | { kind: 'alert'; message: string; options: AlertOptions; resolve: () => void }
    | { kind: 'confirm'; message: string; options: ConfirmOptions; resolve: (ok: boolean) => void }
    | null;

const TYPE_META: Record<DialogType, { color: string; icon: ReactNode }> = {
    info: { color: 'var(--info)', icon: <IconInfoCircle size={14} stroke={2} /> },
    success: { color: 'var(--success)', icon: <IconCheck size={14} stroke={2.4} /> },
    warning: { color: 'var(--warning)', icon: <IconAlertTriangle size={14} stroke={2} /> },
    error: { color: 'var(--error)', icon: <IconX size={14} stroke={2.4} /> },
};

interface ModalProps {
    open: boolean;
    children: ReactNode;
    onClose: () => void;
    ariaLabelledBy?: string;
    ariaLabel?: string;
    className?: string;
    style?: CSSProperties;
    closeOnEscape?: boolean;
    closeOnBackdrop?: boolean;
    onAfterClose?: () => void;
}

// Keep this in sync with --app-overlay-duration. The portal deliberately
// remains mounted for the whole leave transition; consumers only own `open`
// and never need to delay clearing their business state themselves.
const OVERLAY_TRANSITION_MS = 260;

function useOverlayPresence(open: boolean, onAfterClose?: () => void) {
    const [mounted, setMounted] = useState(open);
    const [visible, setVisible] = useState(false);
    const afterCloseRef = useRef(onAfterClose);

    useEffect(() => {
        afterCloseRef.current = onAfterClose;
    }, [onAfterClose]);

    useEffect(() => {
        if (open) {
            setMounted(true);
            // Two frames are intentional: the first commits the mounted,
            // closed pose; the second lets the browser transition to open.
            // A single frame can be batched with the mount and skip enter
            // motion entirely.
            let openFrame = 0;
            const mountFrame = window.requestAnimationFrame(() => {
                openFrame = window.requestAnimationFrame(() => setVisible(true));
            });
            return () => {
                window.cancelAnimationFrame(mountFrame);
                if (openFrame) window.cancelAnimationFrame(openFrame);
            };
        }
        if (!mounted) return;
        setVisible(false);
        const timer = window.setTimeout(() => {
            setMounted(false);
            afterCloseRef.current?.();
        }, OVERLAY_TRANSITION_MS);
        return () => window.clearTimeout(timer);
    }, [mounted, open]);

    return { mounted, visible };
}

/**
 * Shared modal frame for application dialogs.
 * Backdrop clicks close ordinary dialogs by default. Strong configuration
 * flows can opt out with closeOnBackdrop={false}.
 */
export function Modal({
    open,
    children,
    onClose,
    ariaLabelledBy,
    ariaLabel,
    className = '',
    style,
    closeOnEscape = true,
    closeOnBackdrop = true,
    onAfterClose,
}: ModalProps) {
    const dialogRef = useRef<HTMLElement>(null);
    const onCloseRef = useRef(onClose);
    const { mounted, visible } = useOverlayPresence(open, onAfterClose);

    useEffect(() => {
        onCloseRef.current = onClose;
    }, [onClose]);

    useEffect(() => {
        if (!mounted) return;
        const previousOverflow = document.body.style.overflow;
        const focusTimer = window.setTimeout(() => dialogRef.current?.focus(), 0);
        const onKeyDown = (event: KeyboardEvent) => {
            if (event.key === 'Escape' && open && closeOnEscape) onCloseRef.current();
        };
        document.body.style.overflow = 'hidden';
        window.addEventListener('keydown', onKeyDown);
        return () => {
            window.clearTimeout(focusTimer);
            document.body.style.overflow = previousOverflow;
            window.removeEventListener('keydown', onKeyDown);
        };
    }, [closeOnEscape, mounted, open]);

    if (!mounted || typeof document === 'undefined') return null;

    return createPortal(
        <div
            className="app-modal-overlay"
            data-state={visible ? 'open' : 'closed'}
            onMouseDown={(event) => {
                if (open && closeOnBackdrop && event.target === event.currentTarget) onCloseRef.current();
            }}
        >
            <section
                ref={dialogRef}
                className={`app-modal-surface${className ? ` ${className}` : ''}`}
                role="dialog"
                aria-modal="true"
                aria-labelledby={ariaLabelledBy}
                aria-label={ariaLabel}
                tabIndex={-1}
                style={style}
            >
                {children}
            </section>
        </div>,
        document.body,
    );
}

type DrawerProps = Omit<ModalProps, 'style'> & { style?: CSSProperties };

/** Shared right-side drawer with the same overlay and motion contract as Modal. */
export function Drawer({
    open,
    children,
    onClose,
    ariaLabelledBy,
    ariaLabel,
    className = '',
    style,
    closeOnEscape = true,
    closeOnBackdrop = true,
    onAfterClose,
}: DrawerProps) {
    const drawerRef = useRef<HTMLElement>(null);
    const onCloseRef = useRef(onClose);
    const { mounted, visible } = useOverlayPresence(open, onAfterClose);

    useEffect(() => {
        onCloseRef.current = onClose;
    }, [onClose]);

    useEffect(() => {
        if (!mounted) return;
        const previousOverflow = document.body.style.overflow;
        const focusTimer = window.setTimeout(() => drawerRef.current?.focus(), 0);
        const onKeyDown = (event: KeyboardEvent) => {
            if (event.key === 'Escape' && open && closeOnEscape) onCloseRef.current();
        };
        document.body.style.overflow = 'hidden';
        window.addEventListener('keydown', onKeyDown);
        return () => {
            window.clearTimeout(focusTimer);
            document.body.style.overflow = previousOverflow;
            window.removeEventListener('keydown', onKeyDown);
        };
    }, [closeOnEscape, mounted, open]);

    if (!mounted || typeof document === 'undefined') return null;

    return createPortal(
        <div
            className="app-drawer-overlay"
            data-state={visible ? 'open' : 'closed'}
            onMouseDown={(event) => {
                if (open && closeOnBackdrop && event.target === event.currentTarget) onCloseRef.current();
            }}
        >
            <aside
                ref={drawerRef}
                className={`app-drawer-surface${className ? ` ${className}` : ''}`}
                role="dialog"
                aria-modal="true"
                aria-labelledby={ariaLabelledBy}
                aria-label={ariaLabel}
                tabIndex={-1}
                style={style}
            >
                {children}
            </aside>
        </div>,
        document.body,
    );
}

export function DialogProvider({ children }: { children: ReactNode }) {
    const [state, setState] = useState<ModalState>(null);

    const alert = useCallback(
        (message: string, options: AlertOptions = {}) =>
            new Promise<void>((resolve) => setState({ kind: 'alert', message, options, resolve })),
        [],
    );

    const confirm = useCallback(
        (message: string, options: ConfirmOptions = {}) =>
            new Promise<boolean>((resolve) => setState({ kind: 'confirm', message, options, resolve })),
        [],
    );

    const close = useCallback((result?: boolean) => {
        setState((s) => {
            if (!s) return null;
            if (s.kind === 'alert') s.resolve();
            else s.resolve(!!result);
            return null;
        });
    }, []);

    return (
        <DialogContext.Provider value={{ alert, confirm }}>
            {children}
            {state && <DialogModal state={state} onClose={close} />}
        </DialogContext.Provider>
    );
}

function DialogModal({ state, onClose }: { state: NonNullable<ModalState>; onClose: (result?: boolean) => void }) {
    const { t } = useTranslation();
    const btnRef = useRef<HTMLButtonElement>(null);
    const [showDetails, setShowDetails] = useState(false);
    const [open, setOpen] = useState(true);
    const closeResult = useRef(false);

    const requestClose = useCallback((result = false) => {
        closeResult.current = result;
        setOpen(false);
    }, []);

    useEffect(() => {
        const timer = setTimeout(() => btnRef.current?.focus(), 50);
        const onKey = (e: KeyboardEvent) => {
            if (e.key === 'Enter' && state.kind === 'alert') requestClose(true);
        };
        window.addEventListener('keydown', onKey);
        return () => { clearTimeout(timer); window.removeEventListener('keydown', onKey); };
    }, [requestClose, state]);

    const isConfirm = state.kind === 'confirm';
    const type: DialogType = isConfirm
        ? (state.options.danger ? 'error' : 'info')
        : (state.options.type ?? 'info');
    const meta = TYPE_META[type];
    const title = state.options.title
        ?? (isConfirm
            ? t('dialog.confirmTitle', 'Please confirm')
            : type === 'error'
                ? t('dialog.errorTitle', 'Something went wrong')
                : type === 'success'
                    ? t('dialog.successTitle', 'Success')
                    : type === 'warning'
                        ? t('dialog.warningTitle', 'Notice')
                        : t('dialog.infoTitle', 'Notice'));
    const details = !isConfirm ? state.options.details : undefined;

    return (
        <Modal
            open={open}
            onClose={() => requestClose(false)}
            onAfterClose={() => onClose(closeResult.current)}
            ariaLabel={title}
            className="app-modal-surface--compact"
        >
                <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '12px' }}>
                    <span
                        aria-hidden
                        style={{
                            width: '22px', height: '22px', borderRadius: '50%',
                            background: meta.color, color: '#fff',
                            display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
                            fontSize: '13px', fontWeight: 700, flexShrink: 0,
                        }}
                    >{meta.icon}</span>
                    <h4 style={{ margin: 0, fontSize: '15px', fontWeight: 600 }}>{title}</h4>
                </div>
                <div style={{ fontSize: '13px', color: 'var(--text-secondary)', lineHeight: 1.6, marginBottom: details ? '12px' : '20px', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                    {state.message}
                </div>
                {details && (
                    <div style={{ marginBottom: '20px' }}>
                        <button
                            type="button"
                            onClick={() => setShowDetails((v) => !v)}
                            style={{
                                background: 'none', border: 'none', padding: 0,
                                color: 'var(--text-tertiary)', fontSize: '12px',
                                cursor: 'pointer', textDecoration: 'underline',
                            }}
                        >
                            {showDetails ? t('dialog.hideDetails', 'Hide details') : t('dialog.showDetails', 'Show details')}
                        </button>
                        {showDetails && (
                            <pre style={{
                                marginTop: '8px',
                                padding: '10px 12px',
                                background: 'var(--bg-tertiary)',
                                border: '1px solid var(--border-subtle)',
                                borderRadius: '6px',
                                fontSize: '11px',
                                color: 'var(--text-secondary)',
                                maxHeight: '240px',
                                overflow: 'auto',
                                whiteSpace: 'pre-wrap',
                                wordBreak: 'break-all',
                                fontFamily: 'var(--font-mono)',
                            }}>{details}</pre>
                        )}
                    </div>
                )}
                <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '8px' }}>
                    {isConfirm && (
                        <Button type="button" variant="secondary" onClick={() => requestClose(false)}>
                            {state.options.cancelLabel ?? t('common.cancel', 'Cancel')}
                        </Button>
                    )}
                    <Button
                        ref={btnRef}
                        type="button"
                        variant={isConfirm && state.options.danger ? 'danger' : 'primary'}
                        onClick={() => requestClose(true)}
                    >
                        {isConfirm
                            ? (state.options.confirmLabel ?? t('common.confirm', 'Confirm'))
                            : (state.options.confirmLabel ?? t('common.confirm', 'Confirm'))}
                    </Button>
                </div>
        </Modal>
    );
}

export function useDialog() {
    const ctx = useContext(DialogContext);
    if (!ctx) throw new Error('useDialog must be used within DialogProvider');
    return ctx;
}
