export type ResumeEventGate<T> = {
    generation: number;
    runtimeKey: string;
    events: T[];
};

export function createResumeEventGate<T>(
    runtimeKey: string,
    generation: number,
): ResumeEventGate<T> {
    return { runtimeKey, generation, events: [] };
}

export function bufferResumeEvent<T>(
    gate: ResumeEventGate<T> | null,
    runtimeKey: string,
    event: T,
): boolean {
    if (!gate || gate.runtimeKey !== runtimeKey) return false;
    gate.events.push(event);
    return true;
}

export function drainResumeEventGate<T>(gate: ResumeEventGate<T>): T[] {
    const events = gate.events;
    gate.events = [];
    return events;
}

export function createOrReuseResumeEventGate<T>(
    activeGate: ResumeEventGate<T> | null,
    runtimeKey: string,
    generation: number,
): { gate: ResumeEventGate<T>; displacedEvents: T[] } {
    if (activeGate?.runtimeKey === runtimeKey) {
        return { gate: activeGate, displacedEvents: [] };
    }
    return {
        gate: createResumeEventGate<T>(runtimeKey, generation),
        displacedEvents: activeGate ? drainResumeEventGate(activeGate) : [],
    };
}

export function shouldScheduleResumeReconnect(options: {
    pageSuspended: boolean;
    unmounted: boolean;
    hidden: boolean;
    socketReadyState: number | null | undefined;
}): boolean {
    if (options.pageSuspended || options.unmounted || options.hidden) return false;
    return options.socketReadyState !== 0 && options.socketReadyState !== 1;
}

export function resolveVisibleTerminalRecoveryAction(options: {
    isActiveRuntime: boolean;
    recoveryNeeded: boolean;
}): 'ignore' | 'continue' | 'clear' {
    if (!options.isActiveRuntime) return 'ignore';
    return options.recoveryNeeded ? 'continue' : 'clear';
}

export function shouldCompleteRecoveryPolling(options: {
    pollingGeneration: number;
    currentGeneration: number;
    loadedSuccessfully: boolean;
    successfulLoads: number;
    stillActive: boolean;
    socketReadyState: number | null | undefined;
}): boolean {
    return options.pollingGeneration === options.currentGeneration
        && options.loadedSuccessfully
        && options.successfulLoads >= 2
        && options.stillActive
        && options.socketReadyState === 1;
}

/**
 * Remove only transient assistant fragments belonging to the active turn.
 * Durable rows and partial output from an older failed turn must survive.
 */
export function prepareMessagesForActiveTurnResume<
    T extends { role?: string; streaming?: boolean; _streaming?: boolean },
>(messages: T[]): T[] {
    let lastUserIndex = -1;
    for (let index = messages.length - 1; index >= 0; index -= 1) {
        if (messages[index]?.role === 'user') {
            lastUserIndex = index;
            break;
        }
    }
    const prepared = messages.filter((message, index) => (
        index <= lastUserIndex
        || message.role !== 'assistant'
        || (!message.streaming && !message._streaming)
    ));
    return prepared.length === messages.length ? messages : prepared;
}
