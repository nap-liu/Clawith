import { useId, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { IconX } from '@tabler/icons-react';
import { Drawer } from '../Dialog/DialogProvider';
import ToggleSwitch from '../ToggleSwitch';
import Button from './Button';
import './SettingsForm.css';

export function SettingsDrawer({ title, description, children, footer, busy, onClose }: {
    title: string;
    description?: string;
    children: ReactNode;
    footer: ReactNode;
    busy?: boolean;
    onClose: () => void;
}) {
    const titleId = useId();
    const { t } = useTranslation();
    return <Drawer open onClose={onClose} ariaLabelledBy={titleId}
        closeOnEscape={!busy} closeOnBackdrop={!busy} className="settings-drawer">
        <header className="settings-drawer__header">
            <div>
                <h2 id={titleId}>{title}</h2>
                {description && <p>{description}</p>}
            </div>
            <Button variant="ghost" type="button" disabled={busy} onClick={onClose}
                aria-label={t('common.close')}><IconX size={18} /></Button>
        </header>
        <div className="settings-drawer__body">{children}</div>
        <footer className="settings-drawer__footer">{footer}</footer>
    </Drawer>;
}

export function SettingsSection({ title, description, children }: {
    title: string;
    description?: string;
    children: ReactNode;
}) {
    return <section className="settings-section">
        <div className="settings-section__heading">
            <h3>{title}</h3>
            {description && <p>{description}</p>}
        </div>
        {children}
    </section>;
}

export function SettingsField({ label, htmlFor, hint, children }: {
    label: string;
    htmlFor: string;
    hint?: string;
    children: ReactNode;
}) {
    return <div className="settings-field">
        <label className="form-label" htmlFor={htmlFor}>{label}</label>
        {children}
        {hint && <p className="settings-field__hint" id={`${htmlFor}-hint`}>{hint}</p>}
    </div>;
}

export function SettingsToggle({ label, description, checked, onChange, disabled }: {
    label: string;
    description?: string;
    checked: boolean;
    onChange: (value: boolean) => void;
    disabled?: boolean;
}) {
    return <div className="settings-toggle">
        <div>
            <span className="settings-toggle__label">{label}</span>
            {description && <p>{description}</p>}
        </div>
        <ToggleSwitch checked={checked} onChange={onChange} disabled={disabled} ariaLabel={label} />
    </div>;
}
