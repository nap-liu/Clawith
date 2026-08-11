import { IconCheck } from '@tabler/icons-react';
import type { InputHTMLAttributes } from 'react';

import './controls.css';

type CheckboxProps = Omit<InputHTMLAttributes<HTMLInputElement>, 'type'>;

export default function Checkbox({ className = '', checked, ...props }: CheckboxProps) {
    return (
        <span className={`ui-checkbox${checked ? ' is-checked' : ''}`}>
            <input
                type="checkbox"
                checked={checked}
                className={className}
                {...props}
            />
            <IconCheck className="ui-checkbox__check" size={13} stroke={2.5} aria-hidden="true" />
        </span>
    );
}
