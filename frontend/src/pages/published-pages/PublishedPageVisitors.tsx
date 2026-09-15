import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import Pagination from '../../components/Pagination';
import Button from '../../components/ui/Button';
import SearchInput from '../../components/ui/SearchInput';
import { fetchJson } from '../../services/api';
import { formatTime, VISITOR_PAGE_SIZE, type Paged, type Visitor } from './model';
import './PublishedPageVisitors.css';

export default function PublishedPageVisitors({ pageId }: { pageId: string }) {
    const { t } = useTranslation();
    const [draft, setDraft] = useState('');
    const [query, setQuery] = useState('');
    const [page, setPage] = useState(1);
    const params = new URLSearchParams({
        page: String(page), page_size: String(VISITOR_PAGE_SIZE),
    });
    if (query) params.set('q', query);
    const { data, isLoading, isError, refetch } = useQuery({
        queryKey: ['published-pages', 'visitors', pageId, page, query],
        queryFn: () => fetchJson<Paged<Visitor>>(`/pages/${pageId}/visitors?${params}`),
    });

    useEffect(() => {
        if (!data) return;
        const lastPage = Math.max(1, Math.ceil(data.total / VISITOR_PAGE_SIZE));
        if (page > lastPage) setPage(lastPage);
    }, [data, page]);

    function reset() {
        setDraft('');
        setQuery('');
        setPage(1);
    }

    return (
        <section className="published-page-visitors">
            <form className="published-page-visitors__search" onSubmit={event => {
                event.preventDefault();
                setQuery(draft.trim());
                setPage(1);
            }}>
                <SearchInput
                    value={draft}
                    onChange={event => {
                        setDraft(event.target.value);
                        if (!event.target.value) reset();
                    }}
                    placeholder={t('publishedPages.visitors.searchPlaceholder')}
                    aria-label={t('publishedPages.visitors.searchPlaceholder')}
                />
                <Button type="submit" variant="secondary">
                    {t('publishedPages.filters.searchAction')}
                </Button>
                <Button type="button" onClick={reset} disabled={!draft && !query}>
                    {t('publishedPages.filters.resetAction')}
                </Button>
            </form>
            <div aria-live="polite">
                {isLoading ? <p>{t('publishedPages.visitors.loading')}</p> : isError ? (
                    <div role="alert">
                        <p>{t('publishedPages.visitors.loadError')}</p>
                        <Button onClick={() => void refetch()}>{t('publishedPages.visitors.retry')}</Button>
                    </div>
                ) : <>
                    {query && <p className="published-page-visitors__muted">
                        {t('publishedPages.visitors.matches', { count: data?.total || 0 })}
                    </p>}
                    {!data?.items.length ? <p className="published-page-visitors__muted">
                        {t(query ? 'publishedPages.visitors.noMatches' : 'publishedPages.visitors.empty')}
                    </p> : data.items.map(visitor => (
                        <div key={visitor.id} className="published-page-visitors__row">
                            <div className="published-page-visitors__details">
                                <div className="published-page-visitors__identity">
                                    <strong className="published-page-visitors__name" title={visitor.display_name}>
                                        {visitor.display_name}
                                    </strong>
                                    {visitor.email && <span className="published-page-visitors__email" title={visitor.email}>
                                        {visitor.email}
                                    </span>}
                                    {visitor.visitor_type === 'anonymous' && <span className="published-page-visitors__anonymous">
                                        {t('publishedPages.visitors.anonymous')}
                                    </span>}
                                </div>
                                <div className="published-page-visitors__time">
                                    {t('publishedPages.visitors.times', {
                                        first: formatTime(visitor.first_viewed_at),
                                        last: formatTime(visitor.last_viewed_at),
                                    })}
                                </div>
                            </div>
                            <span className="published-page-visitors__count">
                                {t('publishedPages.visitors.views', { count: visitor.view_count })}
                            </span>
                        </div>
                    ))}
                </>}
            </div>
            {!isError && data && data.total > VISITOR_PAGE_SIZE && <Pagination
                page={page} pageSize={VISITOR_PAGE_SIZE} total={data.total} onPageChange={setPage}
            />}
        </section>
    );
}
