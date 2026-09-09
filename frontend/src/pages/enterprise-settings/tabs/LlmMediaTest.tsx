import { useId, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import SelectDropdown from '../../../components/SelectDropdown';
import SubagentRunCard, { parseSubagentRunCardData } from '../../../components/SubagentRunCard';
import Button from '../../../components/ui/Button';
import { TextArea } from '../../../components/ui/TextInput';
import { SettingsDrawer, SettingsField, SettingsSection } from '../../../components/ui/SettingsForm';
import { agentApi } from '../../../services/api';
import { fetchJson } from '../utils/fetchJson';
import type { PoolModel } from './LlmModelForm';

export default function LlmMediaTest({ model, tenantId, onClose }: {
    model: PoolModel;
    tenantId: string;
    onClose: () => void;
}) {
    const { t } = useTranslation();
    const formId = useId();
    const navigate = useNavigate();
    const [purpose, setPurpose] = useState((model.purposes || []).find(p => p !== 'conversation') || 'media_understanding');
    const [agentId, setAgentId] = useState('');
    const [prompt, setPrompt] = useState('');
    const [files, setFiles] = useState('');
    const [submitting, setSubmitting] = useState(false);
    const [receipt, setReceipt] = useState<Record<string, any> | null>(null);
    const [result, setResult] = useState('');
    const [error, setError] = useState('');
    const isSpeech = purpose === 'speech_recognition';
    const agentsQuery = useQuery({
        queryKey: ['media-test-agents', tenantId],
        queryFn: () => agentApi.list(tenantId),
        enabled: Boolean(tenantId) && !isSpeech,
    });
    const eligible = (agentsQuery.data || []).filter(agent => agent.agent_type === 'native'
        && (agent as typeof agent & { scope?: string }).scope !== 'project'
        && String(agent.access_level || '') !== 'read');
    const selectedAgent = agentId || eligible[0]?.id || '';
    const invalid = !isSpeech && (!selectedAgent || !prompt.trim()
        || (purpose === 'media_understanding' && !files.trim()));
    const submit = async () => {
        if (submitting || invalid) return;
        setSubmitting(true);
        setError('');
        setResult('');
        try {
            if (isSpeech) {
                const response = await fetchJson<{ success: boolean }>(
                    `/speech-config/test?model_id=${encodeURIComponent(model.id)}&tenant_id=${encodeURIComponent(tenantId)}`,
                    { method: 'POST' },
                );
                if (response.success) setResult(t('enterprise.llm.connectionPassed'));
                else setError(t('enterprise.llm.testFailedShort'));
            } else {
                setReceipt(await fetchJson(`/enterprise/llm-models/${model.id}/media-test`, {
                    method: 'POST', body: JSON.stringify({ agent_id: selectedAgent, purpose, prompt,
                        files: files.split('\n').map(line => line.trim()).filter(Boolean) }),
                }));
            }
        } catch (failure) {
            setError(String(failure));
        } finally {
            setSubmitting(false);
        }
    };
    return <SettingsDrawer title={t('enterprise.llm.mediaTest')} description={model.label || model.model}
        busy={submitting} onClose={onClose} footer={<>
            <Button variant="secondary" disabled={submitting} onClick={onClose}>{t('common.close')}</Button>
            <Button variant="primary" type="submit" form={formId} disabled={submitting || invalid}>
                {t(submitting ? 'enterprise.llm.testing' : 'enterprise.llm.startMediaTest')}
            </Button>
        </>}>
        <form id={formId} onSubmit={event => { event.preventDefault(); void submit(); }} aria-busy={submitting}>
            {error && <p className="model-pool__error" role="alert">{error}</p>}
            <SettingsSection title={t('enterprise.llm.testSettings')}>
                <SettingsField htmlFor={`${formId}-purpose`} label={t('enterprise.llm.purposesLabel')}>
                    <SelectDropdown value={purpose} disabled={submitting} style={{ width: '100%' }}
                        options={(model.purposes || []).filter(p => p !== 'conversation').map(value => ({ value, label: t(`enterprise.llm.purposes.${value}`) }))}
                        onChange={value => {
                            setPurpose(value as typeof purpose);
                            setError('');
                            setResult('');
                            setReceipt(null);
                        }} ariaLabel={t('enterprise.llm.purposesLabel')} />
                </SettingsField>
                {!isSpeech && <>
                    <SettingsField htmlFor={`${formId}-agent`} label={t('enterprise.llm.testAgent')}>
                        <SelectDropdown value={selectedAgent} options={eligible.map(agent => ({ value: agent.id, label: agent.name }))}
                            disabled={submitting || agentsQuery.isPending || agentsQuery.isError} style={{ width: '100%' }}
                            onChange={setAgentId} ariaLabel={t('enterprise.llm.testAgent')}
                            placeholder={t('enterprise.llm.selectTestAgent')} />
                    </SettingsField>
                    <SettingsField htmlFor={`${formId}-prompt`} label={t('enterprise.llm.testPrompt')}>
                        <TextArea id={`${formId}-prompt`} rows={4} value={prompt} disabled={submitting}
                            onChange={event => setPrompt(event.target.value)} />
                    </SettingsField>
                    <SettingsField htmlFor={`${formId}-files`} label={t('enterprise.llm.testFiles')}
                        hint={t('enterprise.llm.testFilesHint')}>
                        <TextArea id={`${formId}-files`} rows={3} value={files} disabled={submitting}
                            aria-describedby={`${formId}-files-hint`} onChange={event => setFiles(event.target.value)} />
                    </SettingsField>
                </>}
            </SettingsSection>
            {(receipt || result) && <SettingsSection title={t('enterprise.llm.testResult')}>
                {result && <p className="model-pool__status" role="status">{result}</p>}
                {receipt && <SubagentRunCard agentId={receipt.agent_id}
                    data={parseSubagentRunCardData({ toolResult: receipt }, {})} t={t}
                    onOpenSession={data => navigate(`/agents/${receipt.agent_id}/chat?session_id=${data.sessionId}`)} />}
            </SettingsSection>}
        </form>
    </SettingsDrawer>;
}
