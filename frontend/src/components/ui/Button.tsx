import { forwardRef, type ButtonHTMLAttributes } from 'react';

type ButtonVariant = 'default' | 'primary' | 'secondary' | 'ghost' | 'danger';

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
    variant?: ButtonVariant;
};

const variantClasses: Record<ButtonVariant, string> = {
    default: '',
    primary: 'btn-primary',
    secondary: 'btn-secondary',
    ghost: 'btn-ghost',
    danger: 'btn-danger',
};

const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
    { variant = 'default', className = '', ...props },
    ref,
) {
    return (
        <button
            ref={ref}
            className={['btn', variantClasses[variant], className].filter(Boolean).join(' ')}
            {...props}
        />
    );
});

export default Button;
