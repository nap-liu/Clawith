import { useCallback, useEffect } from 'react';
import { useBeforeUnload, useBlocker } from 'react-router-dom';

export function useUnsavedChangesGuard(active: boolean, message: string) {
    const shouldBlock = useCallback(
        ({ currentLocation, nextLocation }: {
            currentLocation: { pathname: string; search: string; hash: string };
            nextLocation: { pathname: string; search: string; hash: string };
        }) => active && (
            currentLocation.pathname !== nextLocation.pathname
            || currentLocation.search !== nextLocation.search
            || currentLocation.hash !== nextLocation.hash
        ),
        [active],
    );
    const blocker = useBlocker(shouldBlock);

    useBeforeUnload(useCallback((event: BeforeUnloadEvent) => {
        if (!active) return;
        event.preventDefault();
        event.returnValue = '';
    }, [active]));

    useEffect(() => {
        if (blocker.state !== 'blocked') return;
        if (window.confirm(message)) blocker.proceed();
        else blocker.reset();
    }, [blocker, message]);
}
