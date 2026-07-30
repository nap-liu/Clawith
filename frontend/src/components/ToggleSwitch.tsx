import * as Switch from '@radix-ui/react-switch';

import './ToggleSwitch.css';

type ToggleSwitchProps = {
    checked: boolean;
    onChange: (checked: boolean) => void;
    disabled?: boolean;
    mixed?: boolean;
    title?: string;
    ariaLabel: string;
};

export default function ToggleSwitch({
    checked,
    onChange,
    disabled = false,
    mixed = false,
    title,
    ariaLabel,
}: ToggleSwitchProps) {
    return (
        <Switch.Root
            className={`toggle-switch${mixed ? ' is-mixed' : ''}`}
            checked={checked}
            onCheckedChange={onChange}
            disabled={disabled}
            title={title}
            aria-label={ariaLabel}
            aria-checked={mixed ? 'mixed' : checked}
        >
            <Switch.Thumb className="toggle-switch__knob" />
        </Switch.Root>
    );
}
