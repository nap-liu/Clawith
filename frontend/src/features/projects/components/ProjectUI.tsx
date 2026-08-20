import {
    forwardRef,
    useEffect,
    useId,
    useRef,
    type ButtonHTMLAttributes,
    type HTMLAttributes,
    type ReactNode,
    type TableHTMLAttributes,
    type TdHTMLAttributes,
    type TextareaHTMLAttributes,
    type ThHTMLAttributes,
} from 'react';

import Button from '../../../components/ui/Button';
import Checkbox from '../../../components/ui/Checkbox';
import SearchInput from '../../../components/ui/SearchInput';
import TextInput from '../../../components/ui/TextInput';
import SelectDropdown, { type SelectDropdownOption } from '../../../components/SelectDropdown';
import ToggleSwitch from '../../../components/ToggleSwitch';

import './ProjectUI.css';

export { Button, Checkbox, SearchInput, TextInput, ToggleSwitch };

export type ProjectSelectOption<T extends string> = SelectDropdownOption<T>;

type ProjectFieldProps = {
    label: ReactNode;
    children: ReactNode;
    hint?: ReactNode;
    error?: ReactNode;
    required?: boolean;
    labelFor?: string;
    className?: string;
};

export function ProjectField({
    label,
    children,
    hint,
    error,
    required = false,
    labelFor,
    className = '',
}: ProjectFieldProps) {
    return (
        <div className={['project-field', 'pm-field', error ? 'is-invalid' : '', className].filter(Boolean).join(' ')}>
            <label className="project-field__label" htmlFor={labelFor}>
                {label}
                {required && <em aria-hidden="true">*</em>}
            </label>
            {children}
            {error ? <small className="project-field__error" role="alert">{error}</small> : hint ? <small>{hint}</small> : null}
        </div>
    );
}

type ProjectSelectProps<T extends string> = {
    value: T;
    options: readonly ProjectSelectOption<T>[];
    onChange: (value: T) => void;
    ariaLabel: string;
    disabled?: boolean;
    className?: string;
    placeholder?: string;
};

export function ProjectSelect<T extends string>(props: ProjectSelectProps<T>) {
    return <SelectDropdown {...props} className={['project-select', props.className].filter(Boolean).join(' ')} />;
}

export const ProjectTextarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(
    function ProjectTextarea({ className = '', ...props }, ref) {
        return <textarea ref={ref} className={['project-textarea', 'form-input', className].filter(Boolean).join(' ')} {...props} />;
    },
);

type ProjectDialogProps = {
    open: boolean;
    onClose: () => void;
    ariaLabel: string;
    children: ReactNode;
    className?: string;
};

export function ProjectDialog({ open, onClose, ariaLabel, children, className = '' }: ProjectDialogProps) {
    const dialogRef = useRef<HTMLDialogElement>(null);

    useEffect(() => {
        const dialog = dialogRef.current;
        if (!dialog) return;
        if (open && !dialog.open) dialog.showModal();
        if (!open && dialog.open) dialog.close();
    }, [open]);

    if (!open) return null;
    return (
        <dialog
            ref={dialogRef}
            className={['project-dialog', className].filter(Boolean).join(' ')}
            aria-label={ariaLabel}
            onCancel={(event) => {
                event.preventDefault();
                onClose();
            }}
            onClick={(event) => {
                if (event.target === event.currentTarget) onClose();
            }}
        >
            {children}
        </dialog>
    );
}

type ProjectEmptyStateProps = {
    icon?: ReactNode;
    title: ReactNode;
    description?: ReactNode;
    action?: ReactNode;
    tone?: 'default' | 'error';
    className?: string;
};

export function ProjectEmptyState({
    icon,
    title,
    description,
    action,
    tone = 'default',
    className = '',
}: ProjectEmptyStateProps) {
    return (
        <div className={['project-empty-state', 'pm-state', tone === 'error' ? 'pm-state-error' : '', className].filter(Boolean).join(' ')}>
            {icon && <span className="project-empty-state__icon pm-empty-symbol">{icon}</span>}
            <strong>{title}</strong>
            {description && <p>{description}</p>}
            {action}
        </div>
    );
}

export type ProjectSegment<T extends string> = {
    value: T;
    label: ReactNode;
    icon?: ReactNode;
    count?: number;
    disabled?: boolean;
};

export type ProjectRadioOption<T extends string> = {
    value: T;
    label: ReactNode;
    description?: ReactNode;
    disabled?: boolean;
};

type ProjectRadioGroupProps<T extends string> = {
    value: T;
    options: readonly ProjectRadioOption<T>[];
    onChange: (value: T) => void;
    ariaLabel: string;
    className?: string;
};

export function ProjectRadioGroup<T extends string>({ value, options, onChange, ariaLabel, className = '' }: ProjectRadioGroupProps<T>) {
    const radioName = useId();
    return (
        <div className={['project-radio-group', className].filter(Boolean).join(' ')} role="radiogroup" aria-label={ariaLabel}>
            {options.map(option => (
                <label key={option.value} className={['project-radio-option', value === option.value ? 'is-selected' : ''].filter(Boolean).join(' ')}>
                    <input name={radioName} type="radio" checked={value === option.value} disabled={option.disabled} onChange={() => onChange(option.value)} />
                    <span><strong>{option.label}</strong>{option.description && <small>{option.description}</small>}</span>
                </label>
            ))}
        </div>
    );
}

type ProjectSegmentedControlProps<T extends string> = {
    value: T;
    options: readonly ProjectSegment<T>[];
    onChange: (value: T) => void;
    ariaLabel: string;
    className?: string;
    disabled?: boolean;
};

export function ProjectSegmentedControl<T extends string>({
    value,
    options,
    onChange,
    ariaLabel,
    className = '',
    disabled = false,
}: ProjectSegmentedControlProps<T>) {
    return (
        <div className={['project-segmented-control', className].filter(Boolean).join(' ')} role="group" aria-label={ariaLabel}>
            {options.map(option => (
                <Button
                    key={option.value}
                    type="button"
                    variant="ghost"
                    className={value === option.value ? 'is-active' : ''}
                    aria-pressed={value === option.value}
                    disabled={disabled || option.disabled}
                    onClick={() => onChange(option.value)}
                >
                    {option.icon}
                    <span>{option.label}</span>
                    {option.count !== undefined && <ProjectCountBadge>{option.count}</ProjectCountBadge>}
                </Button>
            ))}
        </div>
    );
}

export const ProjectIconButton = forwardRef<HTMLButtonElement, ButtonHTMLAttributes<HTMLButtonElement>>(
    function ProjectIconButton({ className = '', type = 'button', ...props }, ref) {
        return <Button ref={ref} type={type} variant="ghost" className={['project-icon-button', 'pm-icon-button', className].filter(Boolean).join(' ')} {...props} />;
    },
);

type ProjectProgressBarProps = {
    value: number;
    label?: string;
    showValue?: boolean;
    className?: string;
};

export function ProjectProgressBar({ value, label = '项目进度', showValue = true, className = '' }: ProjectProgressBarProps) {
    const normalizedValue = Math.max(0, Math.min(100, value));
    return (
        <div className={['project-progress', className].filter(Boolean).join(' ')}>
            <div
                className="project-progress__track"
                role="progressbar"
                aria-label={label}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={normalizedValue}
            >
                <span style={{ width: `${normalizedValue}%` }} />
            </div>
            {showValue && <strong>{normalizedValue}%</strong>}
        </div>
    );
}

type BadgeTone = 'neutral' | 'info' | 'success' | 'warning' | 'error';

export function ProjectStatusBadge({ children, tone = 'neutral', className = '' }: { children: ReactNode; tone?: BadgeTone; className?: string }) {
    return <span className={['project-status-badge', `is-${tone}`, className].filter(Boolean).join(' ')}><i aria-hidden="true" />{children}</span>;
}

export function ProjectCountBadge({ children, className = '' }: { children: ReactNode; className?: string }) {
    return <span className={['project-count-badge', className].filter(Boolean).join(' ')}>{children}</span>;
}

export function ProjectPanel({ className = '', ...props }: HTMLAttributes<HTMLElement>) {
    return <section className={['project-panel', className].filter(Boolean).join(' ')} {...props} />;
}

export function ProjectCard({ className = '', ...props }: HTMLAttributes<HTMLElement>) {
    return <article className={['project-card', className].filter(Boolean).join(' ')} {...props} />;
}

export function ProjectDataTable({ className = '', ...props }: TableHTMLAttributes<HTMLTableElement>) {
    return <div className="project-data-table-scroll"><table className={['project-data-table', className].filter(Boolean).join(' ')} {...props} /></div>;
}

export function ProjectDataTableHead(props: HTMLAttributes<HTMLTableSectionElement>) {
    return <thead {...props} />;
}

export function ProjectDataTableBody(props: HTMLAttributes<HTMLTableSectionElement>) {
    return <tbody {...props} />;
}

export function ProjectDataTableRow(props: HTMLAttributes<HTMLTableRowElement>) {
    return <tr {...props} />;
}

export function ProjectDataTableHeader(props: ThHTMLAttributes<HTMLTableCellElement>) {
    return <th scope="col" {...props} />;
}

export function ProjectDataTableCell(props: TdHTMLAttributes<HTMLTableCellElement>) {
    return <td {...props} />;
}
