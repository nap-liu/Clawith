import { useRef, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { Modal } from './Dialog/DialogProvider';
import Button from './ui/Button';

interface ConfirmModalProps {
    open: boolean;
    title: string;
    message: string;
    confirmLabel?: string;
    cancelLabel?: string;
    danger?: boolean;
    onConfirm: () => void;
    onCancel: () => void;
}

export default function ConfirmModal({ open, title, message, confirmLabel, cancelLabel, danger, onConfirm, onCancel }: ConfirmModalProps) {
    const { t } = useTranslation();
    const btnRef = useRef<HTMLButtonElement>(null);
    const resolvedConfirmLabel = confirmLabel ?? t('common.confirmActions.confirmLabel');
    const resolvedCancelLabel = cancelLabel ?? t('common.confirmActions.cancelLabel');

    useEffect(() => {
        if (open) setTimeout(() => btnRef.current?.focus(), 100);
    }, [open]);

    return (
        <Modal
            open={open}
            onClose={onCancel}
            ariaLabelledBy="confirm-modal-title"
            className="app-modal-surface--compact"
            style={{ width: 'min(380px, calc(100vw - 40px))' }}
        >
                <h4 id="confirm-modal-title" style={{ marginBottom: '12px', fontSize: '15px' }}>{title}</h4>
                <p style={{ fontSize: '13px', color: 'var(--text-secondary)', marginBottom: '20px', lineHeight: 1.5 }}>{message}</p>
                <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '8px' }}>
                    <Button type="button" variant="secondary" onClick={onCancel}>{resolvedCancelLabel}</Button>
                    <Button ref={btnRef} type="button" variant={danger ? 'danger' : 'primary'} onClick={onConfirm}>{resolvedConfirmLabel}</Button>
                </div>
        </Modal>
    );
}
