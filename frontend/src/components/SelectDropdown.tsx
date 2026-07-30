import * as Select from '@radix-ui/react-select';
import { IconCheck, IconChevronDown } from '@tabler/icons-react';

import './SelectDropdown.css';

export type SelectDropdownOption<T extends string> = {
    value: T;
    label: string;
};

type SelectDropdownProps<T extends string> = {
    value: T;
    options: readonly SelectDropdownOption<T>[];
    onChange: (value: T) => void;
    ariaLabel: string;
    disabled?: boolean;
    className?: string;
};

export default function SelectDropdown<T extends string>({
    value,
    options,
    onChange,
    ariaLabel,
    disabled = false,
    className = '',
}: SelectDropdownProps<T>) {
    return (
        <div className={`select-dropdown ${className}`.trim()}>
            <Select.Root
                value={value}
                onValueChange={(nextValue) => onChange(nextValue as T)}
                disabled={disabled}
            >
                <Select.Trigger className="select-dropdown__trigger" aria-label={ariaLabel}>
                    <Select.Value />
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
                                    value={option.value}
                                    className="select-dropdown__option"
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
