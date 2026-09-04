import { useState } from 'react';
import { useTranslation } from 'react-i18next';

import Button from '../../../components/ui/Button';
import TextInput from '../../../components/ui/TextInput';
import fetchJson from '../api';

type Props = {
    toolId: string;
    displayName: string;
    onSaved: () => void | Promise<void>;
    toast: { error: (message: string, options?: Record<string, unknown>) => void };
};

export default function MCPToolDisplayNameEditor({ toolId, displayName, onSaved, toast }: Props) {
    const { t } = useTranslation();
    const [editing, setEditing] = useState(false);
    const [value, setValue] = useState(displayName);
    const [saving, setSaving] = useState(false);

    if (!editing) {
        return (
            <Button variant="secondary" style={{ padding: '3px 8px', fontSize: 11 }} onClick={() => setEditing(true)}>
                {t('enterprise.tools.renameMcpTool')}
            </Button>
        );
    }

    const save = async () => {
        const nextName = value.trim();
        if (!nextName) return;
        setSaving(true);
        try {
            await fetchJson(`/tools/${toolId}`, {
                method: 'PUT',
                body: JSON.stringify({ display_name: nextName }),
            });
            setEditing(false);
            await onSaved();
        } catch (error: any) {
            toast.error(t('enterprise.tools.renameMcpToolFailed'), {
                details: String(error?.message || error),
            });
        } finally {
            setSaving(false);
        }
    };

    return (
        <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
            <TextInput
                value={value}
                onChange={(event) => setValue(event.target.value)}
                maxLength={200}
                autoFocus
                style={{ width: 160, padding: '4px 7px', fontSize: 11 }}
                aria-label={t('enterprise.tools.renameMcpTool')}
            />
            <Button variant="primary" style={{ padding: '3px 7px', fontSize: 11 }} disabled={!value.trim() || saving} onClick={save}>
                {t('common.save')}
            </Button>
            <Button variant="ghost" style={{ padding: '3px 7px', fontSize: 11 }} disabled={saving} onClick={() => {
                setValue(displayName);
                setEditing(false);
            }}>
                {t('common.cancel')}
            </Button>
        </div>
    );
}
