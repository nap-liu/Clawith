import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { IconWorld } from '@tabler/icons-react';

interface Props {
    /** When provided, replaces the Clawith brand with a "← BACK" pill button */
    onBack?: () => void;
    /** When provided, renders the language toggle in the top-right corner */
    onToggleLang?: () => void;
    /** Page contents */
    children: ReactNode;
    className?: string;
}

export default function AtlasFrame({ onBack, onToggleLang, className, children }: Props) {
    const { t } = useTranslation();
    const pageClass = ['atlas-page', className].filter(Boolean).join(' ');
    // Theme-aware brand mark: white logo on Night Atlas (dark), black on Paper Atlas (light) — mirrors Layout.
    const logoSrc = (typeof window !== 'undefined' && localStorage.getItem('theme') === 'dark')
        ? '/logo-white.png'
        : '/logo-black.png';
    return (
        <div className={pageClass}>
            <header className="atlas-frame-top">
                <div className="atlas-frame-top-left">
                    {onBack ? (
                        <button type="button" className="atlas-back-btn" onClick={onBack}>
                            <span aria-hidden="true">←</span> Back
                        </button>
                    ) : (
                        <span className="atlas-brand-wordmark" style={{ display: 'inline-flex', alignItems: 'center', gap: 9 }}>
                            <img src={logoSrc} alt="" style={{ height: 26, width: 26, display: 'block' }} />
                            <span style={{ fontSize: 17, fontWeight: 700, letterSpacing: '0.02em' }}>{t('app.name')}</span>
                        </span>
                    )}
                </div>
                {onToggleLang && (
                    <button type="button" className="atlas-globe-btn" onClick={onToggleLang} aria-label="Toggle language">
                        <IconWorld size={16} stroke={1.4} />
                    </button>
                )}
            </header>

            <main className="atlas-frame-body">{children}</main>
        </div>
    );
}
