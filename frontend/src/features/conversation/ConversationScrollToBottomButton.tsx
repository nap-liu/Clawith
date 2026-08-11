import type { CSSProperties } from 'react';
import { IconChevronDown } from '@tabler/icons-react';

import './ConversationScrollToBottomButton.css';

export interface ConversationScrollToBottomButtonProps {
    onClick: () => void;
    variant: 'web' | 'h5';
    bottom?: number;
    label?: string;
}

export default function ConversationScrollToBottomButton({
    onClick,
    variant,
    bottom,
    label = '滚动到底',
}: ConversationScrollToBottomButtonProps) {
    const style = bottom == null
        ? undefined
        : ({ '--conversation-scroll-bottom-offset': `${bottom}px` } as CSSProperties);
    return (
        <button
            type="button"
            className={`conversation-scroll-bottom conversation-scroll-bottom--${variant}`}
            style={style}
            onClick={onClick}
            aria-label={label}
            title={label}
        >
            <IconChevronDown size={variant === 'h5' ? 21 : 18} stroke={2} />
        </button>
    );
}
