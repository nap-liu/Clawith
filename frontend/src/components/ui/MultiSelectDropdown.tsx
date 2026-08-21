import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { IconChevronDown, IconX } from '@tabler/icons-react';

import Button from './Button';
import Checkbox from './Checkbox';
import SearchInput from './SearchInput';
import './MultiSelectDropdown.css';

export type MultiSelectOption = {
    value: string;
    label: string;
};

type MultiSelectDropdownProps = {
    options: MultiSelectOption[];
    values: string[];
    onChange: (values: string[]) => void;
    emptyLabel: string;
    selectedLabel: (count: number) => string;
    searchPlaceholder: string;
    noOptionsLabel: string;
    noMatchesLabel: string;
    ariaLabel: string;
    clearLabel?: string;
    className?: string;
};

export default function MultiSelectDropdown({
    options,
    values,
    onChange,
    emptyLabel,
    selectedLabel,
    searchPlaceholder,
    noOptionsLabel,
    noMatchesLabel,
    ariaLabel,
    clearLabel = '清空',
    className = '',
}: MultiSelectDropdownProps) {
    const [open, setOpen] = useState(false);
    const [query, setQuery] = useState('');
    const rootRef = useRef<HTMLDivElement>(null);
    const triggerRef = useRef<HTMLButtonElement>(null);
    const menuId = useId();

    useEffect(() => {
        if (!open) return;
        const closeOnOutsideClick = (event: MouseEvent) => {
            if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
        };
        document.addEventListener('mousedown', closeOnOutsideClick);
        return () => document.removeEventListener('mousedown', closeOnOutsideClick);
    }, [open]);

    const optionLabels = new Map(options.map(option => [option.value, option.label]));
    const label = values.length === 0
        ? emptyLabel
        : values.length === 1
            ? optionLabels.get(values[0]) || selectedLabel(1)
            : selectedLabel(values.length);
    const normalizedQuery = query.trim().toLocaleLowerCase();
    const filteredOptions = useMemo(
        () => normalizedQuery
            ? options.filter(option => option.label.toLocaleLowerCase().includes(normalizedQuery))
            : options,
        [normalizedQuery, options],
    );

    const toggle = (value: string) => {
        onChange(values.includes(value)
            ? values.filter(current => current !== value)
            : [...values, value]);
    };

    return (
        <div ref={rootRef} className={`ui-multi-select ${className}`.trim()}>
            <Button
                ref={triggerRef}
                type="button"
                variant="secondary"
                className="ui-multi-select__trigger"
                aria-label={ariaLabel}
                aria-expanded={open}
                aria-haspopup="dialog"
                aria-controls={menuId}
                onClick={() => setOpen(current => {
                    if (!current) setQuery('');
                    return !current;
                })}
            >
                <span className="ui-multi-select__label">{label}</span>
                <IconChevronDown size={16} aria-hidden="true" />
            </Button>

            {open && (
                <div
                    id={menuId}
                    className="ui-multi-select__menu"
                    role="dialog"
                    aria-label={ariaLabel}
                    onKeyDown={event => {
                        if (event.key !== 'Escape') return;
                        event.preventDefault();
                        setOpen(false);
                        triggerRef.current?.focus();
                    }}
                >
                    <SearchInput
                        className="ui-multi-select__search"
                        value={query}
                        onChange={event => setQuery(event.target.value)}
                        onKeyDown={event => { if (event.key === 'Enter') event.preventDefault(); }}
                        placeholder={searchPlaceholder}
                        aria-label={searchPlaceholder}
                        autoFocus
                    />
                    {values.length > 0 && (
                        <div className="ui-multi-select__actions">
                            <span>已选 {values.length} 项</span>
                            <Button
                                type="button"
                                variant="ghost"
                                className="ui-multi-select__clear"
                                onClick={() => onChange([])}
                            >
                                <IconX size={14} aria-hidden="true" />
                                {clearLabel}
                            </Button>
                        </div>
                    )}
                    <div className="ui-multi-select__options">
                        {options.length === 0 ? (
                            <div className="ui-multi-select__empty">{noOptionsLabel}</div>
                        ) : filteredOptions.length === 0 ? (
                            <div className="ui-multi-select__empty">{noMatchesLabel}</div>
                        ) : filteredOptions.map(option => {
                            const checked = values.includes(option.value);
                            return (
                                <label key={option.value} className="ui-multi-select__option">
                                    <Checkbox
                                        checked={checked}
                                        onChange={() => toggle(option.value)}
                                        aria-label={option.label}
                                    />
                                    <span>{option.label}</span>
                                </label>
                            );
                        })}
                    </div>
                </div>
            )}
        </div>
    );
}
