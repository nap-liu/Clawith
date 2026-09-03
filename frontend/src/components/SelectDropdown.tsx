import * as Select from '@radix-ui/react-select';
import { IconCheck, IconChevronDown } from '@tabler/icons-react';
import type { CSSProperties } from 'react';

import './SelectDropdown.css';

export type SelectDropdownOption<T extends string> = {
    value: T;
    label: string;
    title?: string;
};

type SelectDropdownProps<T extends string> = {
    value: T;
    options: readonly SelectDropdownOption<T>[];
    onChange: (value: T) => void;
    ariaLabel: string;
    disabled?: boolean;
    className?: string;
    placeholder?: string;
    style?: CSSProperties;
};

const EMPTY_OPTION_VALUE = '__select_dropdown_empty_value__';

export default function SelectDropdown<T extends string>({
    value,
    options,
    onChange,
    ariaLabel,
    disabled = false,
    className = '',
    placeholder,
    style,
}: SelectDropdownProps<T>) {
    const hasEmptyOption = options.some((option) => option.value === '');
    const selectValue = value === ''
        ? (hasEmptyOption ? EMPTY_OPTION_VALUE : undefined)
        : value;
    const selectedOption = options.find((option) => option.value === value);

    return (
        <div className={`select-dropdown ${className}`.trim()} style={style}>
            <Select.Root
                value={selectValue}
                onValueChange={(nextValue) => onChange(
                    (nextValue === EMPTY_OPTION_VALUE ? '' : nextValue) as T,
                )}
                disabled={disabled}
            >
                <Select.Trigger
                    className="select-dropdown__trigger"
                    aria-label={ariaLabel}
                    title={selectedOption?.title || selectedOption?.label}
                >
                    <Select.Value placeholder={placeholder} />
                    <Select.Icon asChild>
                        <IconChevronDown size={16} />
                    </Select.Icon>
                </Select.Trigger>
                <Select.Portal>
                    <Select.Content
                        className="select-dropdown__menu"
                        position="popper"
                        sideOffset={4}
                        collisionPadding={8}
                    >
                        <Select.Viewport className="select-dropdown__viewport">
                            {options.map((option) => (
                                <Select.Item
                                    key={option.value}
                                    value={option.value === '' ? EMPTY_OPTION_VALUE : option.value}
                                    className="select-dropdown__option"
                                    title={option.title || option.label}
                                >
                                    <Select.ItemText>{option.label}</Select.ItemText>
                                    <Select.ItemIndicator className="select-dropdown__indicator">
                                        <IconCheck size={16} />
                                    </Select.ItemIndicator>
                                </Select.Item>
                            ))}
                        </Select.Viewport>
                    </Select.Content>
                </Select.Portal>
            </Select.Root>
        </div>
    );
}
