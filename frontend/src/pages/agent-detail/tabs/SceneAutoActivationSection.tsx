import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import ToggleSwitch from '../../../components/ToggleSwitch';
import MultiSelectDropdown from '../../../components/ui/MultiSelectDropdown';
import Button from '../../../components/ui/Button';
import { sceneApi, type SceneAutoActivation, type SceneConversationTarget } from '../../../services/api/scenes';

export default function SceneAutoActivationSection({ agentId, value, disabled, onChange }: {
    agentId: string;
    value?: SceneAutoActivation;
    disabled: boolean;
    onChange: (value: SceneAutoActivation) => void;
}) {
    const { t } = useTranslation();
    const config = value || { enabled: false, targets: [] };
    const [query, setQuery] = useState('');
    const [options, setOptions] = useState<SceneConversationTarget[]>([]);
    const [nextOffset, setNextOffset] = useState<number | null>(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState(false);
    const requestId = useRef(0);

    const load = async (offset = 0) => {
        const id = ++requestId.current;
        setLoading(true);
        setError(false);
        try {
            const result = await sceneApi.conversationOptions(agentId, query, offset);
            if (id !== requestId.current) return;
            setOptions((current) => offset ? [...current, ...result.items] : result.items);
            setNextOffset(result.next_offset);
        } catch {
            if (id === requestId.current) setError(true);
        } finally {
            if (id === requestId.current) setLoading(false);
        }
    };

    useEffect(() => {
        requestId.current += 1;
        setOptions([]);
        setNextOffset(null);
        const timer = window.setTimeout(() => void load(), 250);
        return () => { window.clearTimeout(timer); requestId.current += 1; };
    }, [agentId, query]);

    const cache = new Map([...config.targets, ...options].map((item) => [item.target_ref, item]));
    return (
        <section className="scene-config__panel">
            <div className="scene-config__enabled-control">
                <span>{t('sceneAuto.title')}</span>
                <ToggleSwitch checked={config.enabled} disabled={disabled}
                    ariaLabel={t('sceneAuto.title')}
                    onChange={(enabled) => onChange({ ...config, enabled })} />
            </div>
            <div className="scene-config__panel-title"><span>{t('sceneAuto.description')}</span></div>
            <MultiSelectDropdown
                options={[...cache.values()].filter((item) => !query || options.some((row) => row.target_ref === item.target_ref)).map((item) => ({
                    value: item.target_ref,
                    label: item.label || t('sceneAuto.unnamed'),
                    description: `${t(item.is_group ? 'sceneAuto.group' : 'sceneAuto.private')} · ${item.source_channel}`,
                }))}
                values={config.targets.map((item) => item.target_ref)}
                onChange={(refs) => onChange({ ...config, targets: refs.map((ref) => cache.get(ref)!).filter(Boolean)
                    .map(({ target_ref, label, source_channel, is_group }) => ({ target_ref, label, source_channel, is_group })) })}
                disabled={disabled || !config.enabled}
                onSearchChange={setQuery}
                onLoadMore={nextOffset === null ? undefined : () => void load(nextOffset)}
                loadMoreLabel={t(loading ? 'sceneAuto.loading' : 'sceneAuto.loadMore')}
                loading={loading}
                emptyLabel={t('sceneAuto.select')}
                selectedLabel={(count) => t('sceneAuto.selected', { count })}
                searchPlaceholder={t('sceneAuto.search')}
                noOptionsLabel={t(loading ? 'sceneAuto.loading' : 'sceneAuto.empty')}
                noMatchesLabel={t('sceneAuto.empty')}
                ariaLabel={t('sceneAuto.select')}
                clearLabel={t('sceneAuto.clear')}
            />
            {error && <p role="alert">{t('sceneAuto.loadError')} <Button variant="ghost" onClick={() => void load()}>{t('sceneAuto.retry')}</Button></p>}
        </section>
    );
}
