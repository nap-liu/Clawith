import './PublishedPageAttribution.css';

export type PublishedPageActor = {
    id: string;
    display_name: string;
    email?: string | null;
};

type PublishedPageAttributionProps = {
    createdBy?: PublishedPageActor | null;
    createdAt?: string | null;
    lastPublishedBy?: PublishedPageActor | null;
    lastPublishedAt?: string | null;
    variant?: 'compact' | 'detail';
};

function actorName(actor?: PublishedPageActor | null, fallback = '未知用户') {
    return actor?.display_name || fallback;
}

function formattedTime(value?: string | null, fallback = '未记录') {
    if (!value) return fallback;
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return fallback;
    return new Intl.DateTimeFormat('zh-CN', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        hour12: false,
    }).format(parsed);
}

export default function PublishedPageAttribution({
    createdBy,
    createdAt,
    lastPublishedBy,
    lastPublishedAt,
    variant = 'compact',
}: PublishedPageAttributionProps) {
    const createdTime = formattedTime(createdAt);
    const lastPublisherName = actorName(lastPublishedBy, '历史未记录');
    const lastPublishedTime = formattedTime(lastPublishedAt, '历史未记录');

    if (variant === 'detail') {
        return (
            <dl className="published-page-attribution published-page-attribution--detail">
                <div>
                    <dt>创建人</dt>
                    <dd>
                        <strong>{actorName(createdBy)}</strong>
                        {createdBy?.email && <span title={createdBy.email}>{createdBy.email}</span>}
                        {createdAt && <time dateTime={createdAt}>{createdTime}</time>}
                    </dd>
                </div>
                <div>
                    <dt>最近发布</dt>
                    <dd>
                        <strong>{lastPublisherName}</strong>
                        {lastPublishedBy?.email && <span title={lastPublishedBy.email}>{lastPublishedBy.email}</span>}
                        {lastPublishedAt && <time dateTime={lastPublishedAt}>{lastPublishedTime}</time>}
                    </dd>
                </div>
            </dl>
        );
    }

    return (
        <div
            className="published-page-attribution published-page-attribution--compact"
            aria-label={`创建人 ${actorName(createdBy)}，创建时间 ${createdTime}，最近发布人 ${lastPublisherName}，最近发布时间 ${lastPublishedTime}`}
        >
            <span>创建 <strong>{actorName(createdBy)}</strong>{createdAt && <time dateTime={createdAt}>{createdTime}</time>}</span>
            <i aria-hidden="true" />
            <span>最近发布 <strong>{lastPublisherName}</strong>{lastPublishedAt && <time dateTime={lastPublishedAt}>{lastPublishedTime}</time>}</span>
        </div>
    );
}
