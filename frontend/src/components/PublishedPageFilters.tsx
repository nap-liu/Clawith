import { useTranslation } from 'react-i18next';

import SelectDropdown from './SelectDropdown';
import Button from './ui/Button';
import MultiSelectDropdown from './ui/MultiSelectDropdown';
import SearchInput from './ui/SearchInput';

import './PublishedPageFilters.css';

export type PublishedPageAgentOption = {
    id: string;
    name: string;
};

export type PublishedPageAccessModeFilter = '' | 'public' | 'authenticated' | 'restricted';

type PublishedPageFiltersProps = {
    agents: PublishedPageAgentOption[];
    selectedAgentIds: string[];
    accessMode: PublishedPageAccessModeFilter;
    query: string;
    hasActiveFilters: boolean;
    onAgentChange: (ids: string[]) => void;
    onAccessModeChange: (mode: PublishedPageAccessModeFilter) => void;
    onQueryChange: (value: string) => void;
    onSearch: () => void;
    onReset: () => void;
};

export default function PublishedPageFilters({
    agents,
    selectedAgentIds,
    accessMode,
    query,
    hasActiveFilters,
    onAgentChange,
    onAccessModeChange,
    onQueryChange,
    onSearch,
    onReset,
}: PublishedPageFiltersProps) {
    const { t } = useTranslation();
    const accessModeOptions = [
        { value: '', label: t('publishedPages.filters.allAccessModes') },
        { value: 'public', label: t('publishedPages.accessModes.public') },
        { value: 'authenticated', label: t('publishedPages.accessModes.authenticated') },
        { value: 'restricted', label: t('publishedPages.accessModes.restricted') },
    ] as const;

    return (
        <form
            className="published-page-filters"
            onSubmit={event => { event.preventDefault(); onSearch(); }}
        >
            <MultiSelectDropdown
                className="published-page-filters__agent"
                options={agents.map(agent => ({ value: agent.id, label: agent.name }))}
                values={selectedAgentIds}
                onChange={onAgentChange}
                emptyLabel={t('publishedPages.filters.allAgents')}
                selectedLabel={count => t('publishedPages.filters.selectedAgents', { count })}
                searchPlaceholder={t('publishedPages.filters.agentSearchPlaceholder')}
                noOptionsLabel={t('publishedPages.filters.noAgentOptions')}
                noMatchesLabel={t('publishedPages.filters.noAgentMatches')}
                clearLabel={t('publishedPages.filters.clearAgents')}
                quickClearLabel={t('publishedPages.filters.quickClearAgents')}
                ariaLabel={t('publishedPages.filters.agentAriaLabel')}
            />

            <SelectDropdown
                value={accessMode}
                options={accessModeOptions}
                onChange={onAccessModeChange}
                ariaLabel={t('publishedPages.filters.accessModeAriaLabel')}
                className="published-page-filters__access-mode"
            />

            <SearchInput
                className="published-page-filters__query"
                value={query}
                onChange={event => onQueryChange(event.target.value)}
                placeholder={t('publishedPages.filters.searchPlaceholder')}
                aria-label={t('publishedPages.filters.searchAriaLabel')}
            />
            <div className="published-page-filters__actions">
                <Button type="submit" variant="secondary">
                    {t('publishedPages.filters.searchAction')}
                </Button>
                <Button
                    className="published-page-filters__reset"
                    type="button"
                    onClick={onReset}
                    disabled={!hasActiveFilters}
                >
                    {t('publishedPages.filters.resetAction')}
                </Button>
            </div>
        </form>
    );
}
