import { useMemo } from "react";
import type { ReactNode } from "react";
import { IconCheck, IconChevronDown, IconTools } from "@tabler/icons-react";

import SearchInput from "../ui/SearchInput";
import type { LocalizedToolPresentation } from "../../utils/toolPresentation";

import "./ToolCatalogPanel.css";

export type ToolCatalogPanelGroup<T> = {
  key: string;
  label: string;
  description: string;
  items: T[];
  allItems: T[];
};

type ToolCatalogPanelProps<T> = {
  items: T[];
  allItems?: T[];
  getKey: (item: T) => string;
  getPresentation: (item: T) => LocalizedToolPresentation;
  searchValue: string;
  onSearchChange: (value: string) => void;
  searchPlaceholder: string;
  emptyLabel: string;
  ariaLabel: string;
  expandedGroups: Set<string>;
  onExpandedGroupsChange: (groups: Set<string>) => void;
  selectedKey?: string;
  onSelect?: (key: string) => void;
  selectedKeys?: ReadonlySet<string>;
  onToggle?: (key: string) => void;
  renderGroupIcon?: (group: ToolCatalogPanelGroup<T>) => ReactNode;
  renderGroupSummary?: (group: ToolCatalogPanelGroup<T>) => ReactNode;
  renderGroupActions?: (group: ToolCatalogPanelGroup<T>) => ReactNode;
  renderGroupBody?: (group: ToolCatalogPanelGroup<T>) => ReactNode | undefined;
  renderItemBadges?: (item: T, group: ToolCatalogPanelGroup<T>) => ReactNode;
  renderItemActions?: (item: T, group: ToolCatalogPanelGroup<T>) => ReactNode;
  toolbar?: ReactNode;
  maxHeight?: number;
};

export default function ToolCatalogPanel<T>({
  items,
  allItems = items,
  getKey,
  getPresentation,
  searchValue,
  onSearchChange,
  searchPlaceholder,
  emptyLabel,
  ariaLabel,
  expandedGroups,
  onExpandedGroupsChange,
  selectedKey,
  onSelect,
  selectedKeys,
  onToggle,
  renderGroupIcon,
  renderGroupSummary,
  renderGroupActions,
  renderGroupBody,
  renderItemBadges,
  renderItemActions,
  toolbar,
  maxHeight,
}: ToolCatalogPanelProps<T>) {
  const normalizedSearch = searchValue.trim().toLocaleLowerCase();
  const multiple = selectedKeys !== undefined;
  const selectable = Boolean(onSelect || onToggle);
  const groups = useMemo(() => {
    const groupedAll = new Map<string, T[]>();
    allItems.forEach((item) => {
      const key = getPresentation(item).groupKey;
      groupedAll.set(key, [...(groupedAll.get(key) || []), item]);
    });

    const groupedVisible = new Map<string, T[]>();
    items.forEach((item) => {
      const presentation = getPresentation(item);
      if (normalizedSearch && !presentation.searchText.includes(normalizedSearch)) return;
      groupedVisible.set(presentation.groupKey, [
        ...(groupedVisible.get(presentation.groupKey) || []),
        item,
      ]);
    });

    return [...groupedVisible.entries()]
      .map(([key, groupItems]) => {
        const presentation = getPresentation(groupItems[0]);
        return {
          key,
          label: presentation.groupLabel,
          description: presentation.groupDescription,
          items: [...groupItems].sort((left, right) =>
            getPresentation(left).name.localeCompare(getPresentation(right).name),
          ),
          allItems: groupedAll.get(key) || groupItems,
        };
      })
      .sort((left, right) => left.label.localeCompare(right.label));
  }, [allItems, getPresentation, items, normalizedSearch]);

  const setGroupExpanded = (key: string) => {
    const next = new Set(expandedGroups);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    onExpandedGroupsChange(next);
  };

  return (
    <div className="tool-catalog-panel" aria-label={ariaLabel}>
      <div className="tool-catalog-panel__toolbar">
        <SearchInput
          value={searchValue}
          placeholder={searchPlaceholder}
          aria-label={searchPlaceholder}
          onChange={(event) => onSearchChange(event.target.value)}
        />
        {toolbar}
      </div>
      <div
        className="tool-catalog-panel__groups"
        style={maxHeight ? { maxHeight } : undefined}
      >
        {groups.length ? groups.map((group) => {
          const expanded = expandedGroups.has(group.key) || Boolean(normalizedSearch);
          const customBody = renderGroupBody?.(group);
          return (
            <section key={group.key} className="tool-catalog-panel__group">
              <div className="tool-catalog-panel__group-header">
                <button
                  type="button"
                  className="tool-catalog-panel__group-toggle"
                  aria-expanded={expanded}
                  onClick={() => setGroupExpanded(group.key)}
                >
                  <IconChevronDown
                    className={expanded ? "is-expanded" : ""}
                    size={16}
                    aria-hidden="true"
                  />
                  <span className="tool-catalog-panel__group-icon" aria-hidden="true">
                    {renderGroupIcon?.(group) || <IconTools size={16} />}
                  </span>
                  <span className="tool-catalog-panel__group-copy">
                    <span>
                      <strong>{group.label}</strong>
                      <small>{renderGroupSummary?.(group) || group.items.length}</small>
                    </span>
                    {group.description ? <small>{group.description}</small> : null}
                  </span>
                </button>
                {renderGroupActions ? (
                  <div className="tool-catalog-panel__group-actions">
                    {renderGroupActions(group)}
                  </div>
                ) : null}
              </div>
              {expanded ? (
                customBody ?? (
                  <div
                    className="tool-catalog-panel__items"
                    role={selectable ? "listbox" : "list"}
                    aria-multiselectable={multiple || undefined}
                  >
                    {group.items.map((item) => {
                      const key = getKey(item);
                      const presentation = getPresentation(item);
                      const selected = multiple
                        ? selectedKeys?.has(key) ?? false
                        : key === selectedKey;
                      const copy = (
                        <>
                          {selectable ? (
                            <span
                              className={`tool-catalog-panel__selection${multiple ? " is-multiple" : ""}`}
                              aria-hidden="true"
                            >
                              {multiple && selected ? <IconCheck size={10} /> : null}
                            </span>
                          ) : null}
                          <span className="tool-catalog-panel__item-copy">
                            <span>
                              <strong>{presentation.name}</strong>
                              {renderItemBadges?.(item, group)}
                            </span>
                            {presentation.description ? <small>{presentation.description}</small> : null}
                          </span>
                        </>
                      );
                      return (
                        <div
                          key={key}
                          className={`tool-catalog-panel__item${selected ? " is-selected" : ""}`}
                          role={selectable ? "option" : "listitem"}
                          aria-selected={selectable ? selected : undefined}
                        >
                          {selectable ? (
                            <button
                              type="button"
                              className="tool-catalog-panel__item-select"
                              onClick={() => onToggle ? onToggle(key) : onSelect?.(key)}
                            >
                              {copy}
                            </button>
                          ) : (
                            <div className="tool-catalog-panel__item-main">{copy}</div>
                          )}
                          {renderItemActions ? (
                            <div className="tool-catalog-panel__item-actions">
                              {renderItemActions(item, group)}
                            </div>
                          ) : null}
                        </div>
                      );
                    })}
                  </div>
                )
              ) : null}
            </section>
          );
        }) : (
          <div className="tool-catalog-panel__empty">{emptyLabel}</div>
        )}
      </div>
    </div>
  );
}
