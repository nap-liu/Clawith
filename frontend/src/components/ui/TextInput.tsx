import { forwardRef, type InputHTMLAttributes, type TextareaHTMLAttributes } from 'react';

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

export const TextArea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(
    function TextArea({ className = '', ...props }, ref) {
        return <textarea ref={ref} className={['form-textarea', className].filter(Boolean).join(' ')} {...props} />;
    },
);
