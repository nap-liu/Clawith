import Button from './ui/Button';
import MultiSelectDropdown from './ui/MultiSelectDropdown';
import SearchInput from './ui/SearchInput';

import './PublishedPageFilters.css';

export type PublishedPageAgentOption = {
    id: string;
    name: string;
};

type PublishedPageFiltersProps = {
    agents: PublishedPageAgentOption[];
    selectedAgentIds: string[];
    query: string;
    hasActiveFilters: boolean;
    onAgentChange: (ids: string[]) => void;
    onQueryChange: (value: string) => void;
    onSearch: () => void;
    onReset: () => void;
};

export default function PublishedPageFilters({
    agents,
    selectedAgentIds,
    query,
    hasActiveFilters,
    onAgentChange,
    onQueryChange,
    onSearch,
    onReset,
}: PublishedPageFiltersProps) {
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
                emptyLabel="全部数字员工"
                selectedLabel={count => `已选 ${count} 个数字员工`}
                searchPlaceholder="搜索数字员工"
                noOptionsLabel="暂无可筛选的数字员工"
                noMatchesLabel="没有匹配的数字员工"
                clearLabel="清空已选"
                ariaLabel="筛选数字员工"
            />

            <SearchInput
                className="published-page-filters__query"
                value={query}
                onChange={event => onQueryChange(event.target.value)}
                placeholder="搜索页面标题或源文件路径"
                aria-label="搜索已发布页面"
            />
            <Button type="submit" variant="secondary">搜索</Button>
            <Button type="button" onClick={onReset} disabled={!hasActiveFilters}>重置</Button>
        </form>
    );
}
