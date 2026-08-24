import {
    forwardRef,
    useCallback,
    useEffect,
    useId,
    useImperativeHandle,
    useMemo,
    useRef,
    useState,
    type ClipboardEvent as ReactClipboardEvent,
    type KeyboardEvent as ReactKeyboardEvent,
} from 'react';

import MentionSuggestions, { type MentionSuggestionOption } from './MentionSuggestions';
import { hasMentionFinalizer, parseExactRichMentions } from './richMentionParsing';
import './RichMentionComposer.css';

export type RichMentionComposerHandle = {
    submit: () => void;
    focus: () => void;
};

type RichMentionComposerProps = {
    value: string;
    options: MentionSuggestionOption[];
    maxMentions: number;
    disabled?: boolean;
    placeholder: string;
    onChange: (displayContent: string, mentionIds: string[], hasUnresolvedMentions: boolean) => void;
    onSubmit: (displayContent: string, mentionIds: string[]) => void;
    onPaste?: (event: ReactClipboardEvent<HTMLDivElement>) => void;
    onLimitExceeded?: () => void;
    onUnresolvedSubmit?: () => void;
};

const resolvedMentionSelector = '[data-rich-mention-agent-id]';
const unresolvedMentionSelector = '[data-rich-mention-unresolved]';
const mentionSelector = `${resolvedMentionSelector},${unresolvedMentionSelector}`;
const queryPattern = /(?:^|[^A-Za-z0-9_.+\-])@([^\s@,，。.!！?？;；:：、()（）\[\]{}<>《》"'“”‘’]*)$/u;

function isInside(root: HTMLElement, node: Node | null): node is Node {
    return Boolean(node && (node === root || root.contains(node)));
}

function rangeBeforeCaret(root: HTMLElement): Range | null {
    const selection = window.getSelection();
    if (!selection?.rangeCount || !isInside(root, selection.anchorNode)) return null;
    const range = document.createRange();
    range.selectNodeContents(root);
    range.setEnd(selection.anchorNode, selection.anchorOffset);
    return range;
}

function tokenElement(node: Node | null): HTMLElement | null {
    if (!node) return null;
    const element = node instanceof HTMLElement ? node : node.parentElement;
    return element?.closest<HTMLElement>(mentionSelector) || null;
}

function adjacentNode(range: Range, direction: 'before' | 'after'): Node | null {
    const container = range.startContainer;
    const offset = range.startOffset;
    if (container instanceof Text) {
        if (direction === 'before' && offset > 0) return null;
        if (direction === 'after' && offset < container.data.length) return null;
        return direction === 'before' ? container.previousSibling : container.nextSibling;
    }
    return direction === 'before' ? container.childNodes[offset - 1] || null : container.childNodes[offset] || null;
}

function insertPlainText(root: HTMLElement, text: string) {
    const selection = window.getSelection();
    if (!selection?.rangeCount || !isInside(root, selection.anchorNode)) return;
    const range = selection.getRangeAt(0);
    range.deleteContents();
    const node = document.createTextNode(text);
    range.insertNode(node);
    range.setStartAfter(node);
    range.collapse(true);
    selection.removeAllRanges();
    selection.addRange(range);
}

function createResolvedToken(option: MentionSuggestionOption) {
    const token = document.createElement('span');
    token.className = 'ui-rich-mention-composer__token';
    token.contentEditable = 'false';
    token.dataset.richMentionAgentId = option.value;
    token.textContent = `@${option.label}`;
    return token;
}

function createUnresolvedToken(raw: string) {
    const token = document.createElement('span');
    token.className = 'ui-rich-mention-composer__token ui-rich-mention-composer__token--unresolved';
    token.contentEditable = 'false';
    token.dataset.richMentionUnresolved = 'true';
    token.setAttribute('aria-invalid', 'true');
    token.title = '未匹配到唯一可用的项目数字员工';
    token.textContent = raw;
    return token;
}

function caretOffset(root: HTMLElement) {
    return rangeBeforeCaret(root)?.toString().length ?? null;
}

function restoreCaret(root: HTMLElement, offset: number | null) {
    if (offset === null) return;
    const selection = window.getSelection();
    if (!selection) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let consumed = 0;
    let node = walker.nextNode();
    while (node) {
        const length = node.textContent?.length || 0;
        const containingToken = tokenElement(node);
        if (offset < consumed + length && containingToken) {
            const range = document.createRange();
            range.setStartAfter(containingToken);
            range.collapse(true);
            selection.removeAllRanges();
            selection.addRange(range);
            return;
        }
        if (offset <= consumed + length && !containingToken) {
            const range = document.createRange();
            range.setStart(node, Math.max(0, offset - consumed));
            range.collapse(true);
            selection.removeAllRanges();
            selection.addRange(range);
            return;
        }
        consumed += length;
        node = walker.nextNode();
    }
    const range = document.createRange();
    range.selectNodeContents(root);
    range.collapse(false);
    selection.removeAllRanges();
    selection.addRange(range);
}

const RichMentionComposer = forwardRef<RichMentionComposerHandle, RichMentionComposerProps>(function RichMentionComposer({
    value,
    options,
    maxMentions,
    disabled = false,
    placeholder,
    onChange,
    onSubmit,
    onPaste,
    onLimitExceeded,
    onUnresolvedSubmit,
}, forwardedRef) {
    const rootRef = useRef<HTMLDivElement>(null);
    const unresolvedTargetRef = useRef<HTMLElement | null>(null);
    const composingRef = useRef(false);
    const suggestionId = useId();
    const [query, setQuery] = useState<string | null>(null);
    const [activeIndex, setActiveIndex] = useState(0);
    const [mentionIds, setMentionIds] = useState<string[]>([]);
    const optionById = useMemo(() => new Map(options.map((option) => [option.value, option])), [options]);

    const readValue = useCallback(() => {
        const root = rootRef.current;
        if (!root) return { displayContent: '', mentionIds: [] as string[], hasUnresolvedMentions: false };
        const nextMentionIds: string[] = [];
        root.querySelectorAll<HTMLElement>(resolvedMentionSelector).forEach((token) => {
            const agentId = token.dataset.richMentionAgentId || '';
            const option = optionById.get(agentId);
            if (!option || token.textContent !== `@${option.label}` || (!nextMentionIds.includes(agentId) && nextMentionIds.length >= maxMentions)) {
                token.replaceWith(createUnresolvedToken(token.textContent || '@未知成员'));
                return;
            }
            if (!nextMentionIds.includes(agentId)) nextMentionIds.push(agentId);
        });
        return {
            displayContent: root.innerText.replace(/\u00a0/g, ' '),
            mentionIds: nextMentionIds,
            hasUnresolvedMentions: Boolean(root.querySelector(unresolvedMentionSelector)),
        };
    }, [maxMentions, optionById]);

    const updateQuery = useCallback(() => {
        const root = rootRef.current;
        if (!root) return setQuery(null);
        const before = rangeBeforeCaret(root)?.toString().replace(/\u00a0/g, ' ') || '';
        const match = before.match(queryPattern);
        unresolvedTargetRef.current = null;
        setQuery(match ? match[1] : null);
        if (match) setActiveIndex(0);
    }, []);

    const emitValue = useCallback(() => {
        const next = readValue();
        setMentionIds(next.mentionIds);
        onChange(next.displayContent, next.mentionIds, next.hasUnresolvedMentions);
        updateQuery();
        return next;
    }, [onChange, readValue, updateQuery]);

    const finalizePlainMentions = useCallback(() => {
        const root = rootRef.current;
        if (!root) return readValue();
        const savedOffset = caretOffset(root);
        const existingIds = new Set(readValue().mentionIds);
        const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
            acceptNode: (node) => tokenElement(node) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT,
        });
        const textNodes: Text[] = [];
        let current = walker.nextNode();
        while (current) {
            if (current instanceof Text) textNodes.push(current);
            current = walker.nextNode();
        }

        textNodes.forEach((textNode) => {
            const source = textNode.data;
            const parsed = parseExactRichMentions(source, options);
            if (!parsed.length) return;
            const fragment = document.createDocumentFragment();
            let cursor = 0;
            parsed.forEach((mention) => {
                if (mention.start > cursor) fragment.append(document.createTextNode(source.slice(cursor, mention.start)));
                const option = mention.agentId ? optionById.get(mention.agentId) : undefined;
                const withinLimit = Boolean(option && (existingIds.has(option.value) || existingIds.size < maxMentions));
                if (mention.status === 'resolved' && option && withinLimit) {
                    fragment.append(createResolvedToken(option));
                    existingIds.add(option.value);
                } else {
                    fragment.append(createUnresolvedToken(mention.raw));
                    if (mention.status === 'resolved' && !withinLimit) onLimitExceeded?.();
                }
                cursor = mention.end;
            });
            if (cursor < source.length) fragment.append(document.createTextNode(source.slice(cursor)));
            textNode.replaceWith(fragment);
        });

        root.querySelectorAll<HTMLElement>(unresolvedMentionSelector).forEach((token) => {
            const raw = token.textContent || '';
            const parsed = parseExactRichMentions(raw, options);
            const mention = parsed.length === 1 && parsed[0].raw === raw ? parsed[0] : null;
            const option = mention?.agentId ? optionById.get(mention.agentId) : undefined;
            if (mention?.status === 'resolved' && option && (existingIds.has(option.value) || existingIds.size < maxMentions)) {
                token.replaceWith(createResolvedToken(option));
                existingIds.add(option.value);
            }
        });
        restoreCaret(root, savedOffset);
        return emitValue();
    }, [emitValue, maxMentions, onLimitExceeded, optionById, options, readValue]);

    const submit = useCallback(() => {
        if (composingRef.current) return;
        setQuery(null);
        unresolvedTargetRef.current = null;
        const next = finalizePlainMentions();
        if (next.hasUnresolvedMentions) {
            onUnresolvedSubmit?.();
            return;
        }
        onSubmit(next.displayContent, next.mentionIds);
    }, [finalizePlainMentions, onSubmit, onUnresolvedSubmit]);

    useImperativeHandle(forwardedRef, () => ({
        submit,
        focus: () => rootRef.current?.focus(),
    }), [submit]);

    useEffect(() => {
        const root = rootRef.current;
        if (!root) return;
        const current = readValue().displayContent;
        if (value === '') {
            if (root.childNodes.length) root.replaceChildren();
            setMentionIds([]);
            setQuery(null);
        } else if (document.activeElement !== root && current !== value) {
            root.textContent = value;
            setMentionIds([]);
        }
    }, [readValue, value]);

    useEffect(() => {
        if (!rootRef.current) return;
        finalizePlainMentions();
        // Member snapshots can change while a draft is open. Re-resolve tokens
        // against the current enabled project members without losing the draft.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [optionById]);

    const suggestions = useMemo(() => {
        if (query === null) return [];
        const normalized = query.trim().toLocaleLowerCase();
        return options
            .filter((option) => mentionIds.includes(option.value) || mentionIds.length < maxMentions)
            .filter((option) => !normalized || option.label.toLocaleLowerCase().includes(normalized));
    }, [maxMentions, mentionIds, options, query]);

    useEffect(() => {
        setActiveIndex((current) => Math.min(current, Math.max(0, suggestions.length - 1)));
    }, [suggestions.length]);

    const selectSuggestion = (option: MentionSuggestionOption) => {
        const root = rootRef.current;
        const selection = window.getSelection();
        if (!root || query === null) return;
        if (!mentionIds.includes(option.value) && mentionIds.length >= maxMentions) {
            onLimitExceeded?.();
            setQuery(null);
            return;
        }

        const unresolvedTarget = unresolvedTargetRef.current;
        if (unresolvedTarget && root.contains(unresolvedTarget)) {
            const token = createResolvedToken(option);
            unresolvedTarget.replaceWith(token);
            const spacer = document.createTextNode(' ');
            token.after(spacer);
            const range = document.createRange();
            range.setStartAfter(spacer);
            range.collapse(true);
            selection?.removeAllRanges();
            selection?.addRange(range);
        } else {
            const caretRange = selection?.rangeCount ? selection.getRangeAt(0) : null;
            const caretNode = caretRange?.startContainer;
            const caretOffset = caretRange?.startOffset || 0;
            const typedLength = query.length + 1;
            if (!(caretNode instanceof Text) || caretOffset < typedLength) return;
            const replaceRange = document.createRange();
            replaceRange.setStart(caretNode, caretOffset - typedLength);
            replaceRange.setEnd(caretNode, caretOffset);
            replaceRange.deleteContents();
            const token = createResolvedToken(option);
            replaceRange.insertNode(token);
            const spacer = document.createTextNode(' ');
            token.after(spacer);
            replaceRange.setStartAfter(spacer);
            replaceRange.collapse(true);
            selection?.removeAllRanges();
            selection?.addRange(replaceRange);
        }
        unresolvedTargetRef.current = null;
        setQuery(null);
        emitValue();
        root.focus();
    };

    const handleKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
        if (query !== null && suggestions.length > 0) {
            if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
                event.preventDefault();
                const direction = event.key === 'ArrowDown' ? 1 : -1;
                setActiveIndex((current) => (current + direction + suggestions.length) % suggestions.length);
                return;
            }
            if ((event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) || event.key === 'Tab') {
                event.preventDefault();
                selectSuggestion(suggestions[activeIndex] || suggestions[0]);
                return;
            }
            if (event.key === 'Escape') {
                event.preventDefault();
                setQuery(null);
                unresolvedTargetRef.current = null;
                return;
            }
        }

        if ((event.key === 'Backspace' || event.key === 'Delete') && !event.nativeEvent.isComposing) {
            const selection = window.getSelection();
            if (selection?.rangeCount && isInside(event.currentTarget, selection.anchorNode)) {
                const range = selection.getRangeAt(0);
                if (!range.collapsed) {
                    event.preventDefault();
                    range.deleteContents();
                    range.collapse(true);
                    window.requestAnimationFrame(emitValue);
                    return;
                }
                const adjacent = adjacentNode(range, event.key === 'Backspace' ? 'before' : 'after');
                const token = tokenElement(adjacent);
                if (token && event.currentTarget.contains(token)) {
                    event.preventDefault();
                    token.remove();
                    emitValue();
                    return;
                }
            }
        }

        if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
            event.preventDefault();
            submit();
        }
    };

    const handlePaste = (event: ReactClipboardEvent<HTMLDivElement>) => {
        onPaste?.(event);
        if (event.defaultPrevented) return;
        const plainText = event.clipboardData.getData('text/plain');
        if (!plainText) return;
        event.preventDefault();
        insertPlainText(event.currentTarget, plainText);
        finalizePlainMentions();
    };

    return <>
        <div
            ref={rootRef}
            className="ui-rich-mention-composer"
            role="textbox"
            aria-label={placeholder}
            aria-multiline="true"
            aria-autocomplete="list"
            aria-controls={query !== null && suggestions.length ? suggestionId : undefined}
            aria-expanded={query !== null && suggestions.length > 0}
            aria-activedescendant={query !== null && suggestions.length ? `${suggestionId}-${suggestions[activeIndex]?.value}` : undefined}
            contentEditable={!disabled}
            suppressContentEditableWarning
            data-placeholder={placeholder}
            onInput={(event) => {
                emitValue();
                const inputData = event.nativeEvent instanceof InputEvent ? event.nativeEvent.data || '' : '';
                if (!composingRef.current && hasMentionFinalizer(inputData)) finalizePlainMentions();
            }}
            onClick={(event) => {
                const unresolved = (event.target as HTMLElement).closest<HTMLElement>(unresolvedMentionSelector);
                if (unresolved && event.currentTarget.contains(unresolved)) {
                    unresolvedTargetRef.current = unresolved;
                    setQuery((unresolved.textContent || '').replace(/^@/u, ''));
                    setActiveIndex(0);
                    return;
                }
                updateQuery();
            }}
            onKeyUp={(event) => {
                if (!['ArrowDown', 'ArrowUp', 'Enter', 'Tab', 'Escape'].includes(event.key)) updateQuery();
            }}
            onKeyDown={handleKeyDown}
            onPaste={handlePaste}
            onCompositionStart={() => { composingRef.current = true; }}
            onCompositionEnd={() => {
                composingRef.current = false;
                emitValue();
            }}
            onBlur={() => window.setTimeout(() => {
                finalizePlainMentions();
                setQuery(null);
                unresolvedTargetRef.current = null;
            }, 0)}
        />
        <MentionSuggestions
            id={suggestionId}
            open={query !== null && suggestions.length > 0}
            options={suggestions}
            activeIndex={activeIndex}
            anchorRef={rootRef}
            onActiveIndexChange={setActiveIndex}
            onSelect={selectSuggestion}
        />
    </>;
});

export default RichMentionComposer;
