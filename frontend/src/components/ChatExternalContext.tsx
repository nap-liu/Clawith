import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { IconChevronRight, IconFileText } from '@tabler/icons-react';
import './ChatExternalContext.css';

/** The original JSON is rendered as text, never interpreted as chat instructions or HTML. */
export default function ChatExternalContext({ context }: { context: unknown }) {
    const { t } = useTranslation();
    const [expanded, setExpanded] = useState(false);
    if (context === undefined) return null;

    const source = context && typeof context === 'object' && 'source' in context
        ? context.source
        : undefined;
    const label = source && typeof source === 'object' && 'label' in source
        && typeof source.label === 'string' ? source.label : '';

    return (
        <details
            className="chat-external-context"
            onToggle={(event) => setExpanded(event.currentTarget.open)}
        >
            <summary title={t('chat.externalContextHint')}>
                <IconFileText size={16} aria-hidden="true" />
                <span>{t('chat.externalContext')}</span>
                {label ? <span className="chat-external-context__source">{label}</span> : null}
                <IconChevronRight className="chat-external-context__chevron" size={16} aria-hidden="true" />
            </summary>
            {expanded ? <div className="chat-external-context__body">
                <p>{t('chat.externalContextHint')}</p>
                <pre>{JSON.stringify(context, null, 2)}</pre>
            </div> : null}
        </details>
    );
}
