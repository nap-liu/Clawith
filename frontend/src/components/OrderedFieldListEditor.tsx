import { IconArrowDown, IconArrowUp, IconPlus, IconTrash } from '@tabler/icons-react';

import SelectDropdown, { type SelectDropdownOption } from './SelectDropdown';
import Button from './ui/Button';

type OrderedFieldListEditorProps<T extends string> = {
    values: readonly T[];
    options: readonly SelectDropdownOption<T>[];
    onChange: (values: T[]) => void;
    addLabel: string;
    fieldAriaLabel: (position: number) => string;
    moveUpLabel: string;
    moveDownLabel: string;
    removeLabel: string;
    minItems?: number;
};

export default function OrderedFieldListEditor<T extends string>({
    values,
    options,
    onChange,
    addLabel,
    fieldAriaLabel,
    moveUpLabel,
    moveDownLabel,
    removeLabel,
    minItems = 1,
}: OrderedFieldListEditorProps<T>) {
    const move = (index: number, offset: -1 | 1) => {
        const target = index + offset;
        if (target < 0 || target >= values.length) return;
        const next = [...values];
        [next[index], next[target]] = [next[target], next[index]];
        onChange(next);
    };

    const add = () => {
        const nextOption = options.find((option) => !values.includes(option.value));
        if (nextOption) onChange([...values, nextOption.value]);
    };

    const availableToAdd = options.some((option) => !values.includes(option.value));

    return (
        <div style={{ display: 'grid', gap: '8px' }}>
            {values.map((value, index) => {
                const rowOptions = options.filter(
                    (option) => option.value === value || !values.includes(option.value),
                );
                return (
                    <div key={`${value}-${index}`} style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                        <span style={{ width: '20px', fontSize: '12px', color: 'var(--text-tertiary)', textAlign: 'center' }}>
                            {index + 1}
                        </span>
                        <SelectDropdown
                            value={value}
                            options={rowOptions}
                            onChange={(nextValue) => {
                                const next = [...values];
                                next[index] = nextValue;
                                onChange(next);
                            }}
                            ariaLabel={fieldAriaLabel(index + 1)}
                            style={{ flex: 1 }}
                        />
                        <Button
                            type="button"
                            variant="ghost"
                            className="btn-sm"
                            onClick={() => move(index, -1)}
                            disabled={index === 0}
                            title={moveUpLabel}
                            aria-label={moveUpLabel}
                        >
                            <IconArrowUp size={15} />
                        </Button>
                        <Button
                            type="button"
                            variant="ghost"
                            className="btn-sm"
                            onClick={() => move(index, 1)}
                            disabled={index === values.length - 1}
                            title={moveDownLabel}
                            aria-label={moveDownLabel}
                        >
                            <IconArrowDown size={15} />
                        </Button>
                        <Button
                            type="button"
                            variant="ghost"
                            className="btn-sm"
                            onClick={() => onChange(values.filter((_, itemIndex) => itemIndex !== index))}
                            disabled={values.length <= minItems}
                            title={removeLabel}
                            aria-label={removeLabel}
                        >
                            <IconTrash size={15} />
                        </Button>
                    </div>
                );
            })}
            <div>
                <Button
                    type="button"
                    variant="secondary"
                    className="btn-sm"
                    onClick={add}
                    disabled={!availableToAdd}
                >
                    <IconPlus size={15} />
                    {addLabel}
                </Button>
            </div>
        </div>
    );
}
