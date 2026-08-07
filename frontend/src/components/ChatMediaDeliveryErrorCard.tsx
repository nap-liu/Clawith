import { IconAlertTriangle, IconClockExclamation } from '@tabler/icons-react';

import type { ChatMediaDeliveryError } from '../utils/chatFileDelivery';
import './ChatMediaDeliveryErrorCard.css';

export default function ChatMediaDeliveryErrorCard({ error }: { error: ChatMediaDeliveryError }) {
    const uncertain = error.status === 'unknown';
    const title = uncertain
        ? '媒体发送结果不确定'
        : error.status === 'unsupported'
            ? '当前通道不支持媒体发送'
            : '媒体发送失败';
    const Icon = uncertain ? IconClockExclamation : IconAlertTriangle;

    return (
        <section className={`chat-media-error-card chat-media-error-card--${error.status}`} role="status">
            <span className="chat-media-error-card__icon" aria-hidden="true"><Icon size={19} stroke={1.8} /></span>
            <span className="chat-media-error-card__content">
                <strong>{title}</strong>
                <span>{error.message}</span>
                <small>{error.code}{error.retryable ? ' · 可以重试' : ' · 请勿自动重试'}</small>
            </span>
        </section>
    );
}
