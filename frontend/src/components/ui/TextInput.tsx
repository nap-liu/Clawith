import { forwardRef, type InputHTMLAttributes } from 'react';

type TextInputProps = InputHTMLAttributes<HTMLInputElement>;

const TextInput = forwardRef<HTMLInputElement, TextInputProps>(function TextInput(
    { className = '', type = 'text', ...props },
    ref,
) {
    return (
        <input
            ref={ref}
            type={type}
            className={['form-input', className].filter(Boolean).join(' ')}
            {...props}
        />
    );
});

export default TextInput;
