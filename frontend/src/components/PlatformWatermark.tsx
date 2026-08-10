import { useEffect, useMemo } from 'react';
import { useLocation } from 'react-router-dom';
import { useAuthStore } from '../stores';
import {
    formatPlatformWatermarkText,
    installPlatformWatermark,
} from '../utils/platformWatermark';

export default function PlatformWatermark() {
    const location = useLocation();
    const token = useAuthStore((state) => state.token);
    const user = useAuthStore((state) => state.user);
    const text = useMemo(() => formatPlatformWatermarkText(user), [user]);

    useEffect(() => {
        if (location.pathname.startsWith('/p/')) return;
        if (!token || !text) return;
        try {
            return installPlatformWatermark(text);
        } catch (error) {
            console.warn('Unable to render the platform watermark', error);
        }
    }, [location.pathname, text, token]);

    return null;
}
