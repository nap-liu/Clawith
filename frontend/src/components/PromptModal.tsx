import { useState, useEffect, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { Modal } from './Dialog/DialogProvider';
import Button from './ui/Button';
import TextInput from './ui/TextInput';

interface PromptModalProps {
    open: boolean;
    title: string;
    placeholder?: string;
    onConfirm: (value: string) => void;
    onCancel: () => void;
}

export default function PromptModal({ open, title, placeholder, onConfirm, onCancel }: PromptModalProps) {
    const { t } = useTranslation();
    const [value, setValue] = useState('');
    const inputRef = useRef<HTMLInputElement>(null);

    useEffect(() => {
        if (open) {
            setValue('');
            setTimeout(() => inputRef.current?.focus(), 100);
        }
    }, [open]);

    return (
        <Modal
            open={open}
            onClose={onCancel}
            ariaLabelledBy="prompt-modal-title"
            className="app-modal-surface--compact"
            style={{ width: 'min(400px, calc(100vw - 40px))' }}
        >
                <h4 id="prompt-modal-title" style={{ marginBottom: '16px', fontSize: '15px' }}>{title}</h4>
                <TextInput
                    ref={inputRef}
                    value={value}
                    onChange={e => setValue(e.target.value)}
                    placeholder={placeholder || ''}
                    onKeyDown={e => {
                        if (e.key === 'Enter' && value.trim()) onConfirm(value.trim());
                    }}
                    style={{ width: '100%', marginBottom: '16px' }}
                />
                <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '8px' }}>
                    <Button type="button" variant="secondary" onClick={onCancel}>{t('common.confirmActions.cancelLabel')}</Button>
                    <Button type="button" variant="primary" onClick={() => { if (value.trim()) onConfirm(value.trim()); }}
                        disabled={!value.trim()}>{t('common.confirmActions.confirmLabel')}</Button>
                </div>
        </Modal>
    );
}
