import { useCallback, useEffect, useRef } from 'react';
import { useBeforeUnload, useBlocker } from 'react-router-dom';
import { useDialog } from '../components/Dialog/DialogProvider';
import { useTranslation } from 'react-i18next';

export function useUnsavedChangesGuard(active: boolean, message: string) {
    const { t } = useTranslation();
    const { confirm: confirmDialog } = useDialog();
    const blockerRef = useRef<ReturnType<typeof useBlocker> | null>(null);
    const promptingRef = useRef(false);
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
    blockerRef.current = blocker;

    useBeforeUnload(useCallback((event: BeforeUnloadEvent) => {
        if (!active) return;
        event.preventDefault();
        event.returnValue = '';
    }, [active]));

    useEffect(() => {
        if (blocker.state !== 'blocked' || promptingRef.current) return;
        promptingRef.current = true;
        void confirmDialog(message, {
            title: t('unsavedChanges.title'),
            danger: true,
            confirmLabel: t('unsavedChanges.continue'),
        }).then((confirmed) => {
            promptingRef.current = false;
            const currentBlocker = blockerRef.current;
            if (currentBlocker?.state !== 'blocked') return;
            if (confirmed) currentBlocker.proceed();
            else currentBlocker.reset();
        });
    }, [blocker.state, confirmDialog, message, t]);
}
