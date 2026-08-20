import { createPortal } from 'react-dom';
import { useRef, type RefObject } from 'react';

import Button from './Button';
import { useAnchoredPopoverPosition } from './useAnchoredPopoverPosition';
import './MentionSuggestions.css';

export type MentionSuggestionOption = {
    value: string;
    label: string;
};

type MentionSuggestionsProps = {
    id: string;
    open: boolean;
    options: MentionSuggestionOption[];
    activeIndex: number;
    anchorRef: RefObject<HTMLElement | null>;
    onActiveIndexChange: (index: number) => void;
    onSelect: (option: MentionSuggestionOption) => void;
};

export default function MentionSuggestions({
    id,
    open,
    options,
    activeIndex,
    anchorRef,
    onActiveIndexChange,
    onSelect,
}: MentionSuggestionsProps) {
    const menuRef = useRef<HTMLDivElement>(null);
    const floatingPosition = useAnchoredPopoverPosition({
        open,
        anchorRef,
        popoverRef: menuRef,
        matchAnchorWidth: true,
        minWidth: 240,
        maxWidth: 320,
        maxHeight: 240,
        minimumVisibleHeight: 160,
        contentVersion: options.length,
    });

    if (!open || options.length === 0) return null;

    return createPortal(
        <div
            ref={menuRef}
            id={id}
            role="listbox"
            aria-label="选择要提及的项目 Agent"
            className="ui-mention-suggestions"
            data-placement={floatingPosition.placement}
            style={floatingPosition.style}
        >
            <div className="ui-mention-suggestions__hint">选择后才会唤醒</div>
            {options.map((option, index) => (
                <Button
                    key={option.value}
                    id={`${id}-${option.value}`}
                    type="button"
                    variant="ghost"
                    role="option"
                    aria-selected={index === activeIndex}
                    className={index === activeIndex ? 'is-active' : ''}
                    onMouseEnter={() => onActiveIndexChange(index)}
                    onMouseDown={(event) => event.preventDefault()}
                    onClick={() => onSelect(option)}
                >
                    <span className="ui-mention-suggestions__avatar" aria-hidden="true">{option.label.slice(0, 1)}</span>
                    <span><strong>{option.label}</strong><small>@{option.label}</small></span>
                </Button>
            ))}
        </div>,
        document.body,
    );
}
