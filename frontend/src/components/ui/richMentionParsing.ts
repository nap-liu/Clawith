export type RichMentionMatchOption = {
    value: string;
    label: string;
    enabled?: boolean;
};

export type ParsedRichMention = {
    start: number;
    end: number;
    raw: string;
    label: string;
    status: 'resolved' | 'unresolved';
    agentId?: string;
    reason?: 'ambiguous' | 'unavailable' | 'unmatched';
};

const boundaryPattern = /[\s,，。.!！?？;；:：、()（）\[\]{}<>《》"'“”‘’@]/u;

function isBoundary(character: string | undefined) {
    return character === undefined || boundaryPattern.test(character);
}

function canStartMention(previousCharacter: string | undefined) {
    // Chinese prose commonly writes “请@研发处理” without a space. Block only
    // ASCII word/email continuations so addresses such as foo@bar.com remain text.
    return previousCharacter === undefined || !/[A-Za-z0-9_.+\-]/u.test(previousCharacter);
}

/**
 * Parse explicit @name spans. Resolution is intentionally strict: the whole
 * name must exactly match one enabled option. Prefixes and duplicate labels
 * remain unresolved so they can never wake an unintended Agent.
 */
export function parseExactRichMentions(
    text: string,
    options: RichMentionMatchOption[],
): ParsedRichMention[] {
    const results: ParsedRichMention[] = [];
    const labels = Array.from(new Set(options.map((option) => option.label.trim()).filter(Boolean)))
        .sort((left, right) => right.length - left.length);

    for (let index = 0; index < text.length; index += 1) {
        if (text[index] !== '@' || !canStartMention(text[index - 1])) continue;
        const nameStart = index + 1;
        if (nameStart >= text.length) continue;

        const matchedLabel = labels.find((label) => (
            text.startsWith(label, nameStart)
            && isBoundary(text[nameStart + label.length])
        ));

        if (matchedLabel) {
            const candidates = options.filter((option) => option.label.trim() === matchedLabel);
            const enabledCandidates = Array.from(new Map(
                candidates
                    .filter((option) => option.enabled !== false)
                    .map((option) => [option.value, option]),
            ).values());
            const end = nameStart + matchedLabel.length;
            if (enabledCandidates.length === 1) {
                results.push({
                    start: index,
                    end,
                    raw: text.slice(index, end),
                    label: matchedLabel,
                    status: 'resolved',
                    agentId: enabledCandidates[0].value,
                });
            } else {
                results.push({
                    start: index,
                    end,
                    raw: text.slice(index, end),
                    label: matchedLabel,
                    status: 'unresolved',
                    reason: enabledCandidates.length > 1 ? 'ambiguous' : 'unavailable',
                });
            }
            index = end - 1;
            continue;
        }

        let end = nameStart;
        while (end < text.length && !isBoundary(text[end])) end += 1;
        if (end === nameStart) continue;
        results.push({
            start: index,
            end,
            raw: text.slice(index, end),
            label: text.slice(nameStart, end),
            status: 'unresolved',
            reason: 'unmatched',
        });
        index = end - 1;
    }

    return results;
}

export function hasMentionFinalizer(text: string) {
    const lastCharacter = text.length ? text[text.length - 1] : undefined;
    return Boolean(lastCharacter && isBoundary(lastCharacter) && lastCharacter !== '@');
}
