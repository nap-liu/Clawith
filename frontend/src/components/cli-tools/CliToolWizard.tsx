import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { CliTool } from './types';
import { defaultCliToolConfig } from './types';
import { cliToolsApi } from './api';
import { BasicInfoStep } from './steps/BasicInfoStep';
import { BinaryStep } from './steps/BinaryStep';
import { ConfigStep } from './steps/ConfigStep';

type Step = 1 | 2 | 3;

export function CliToolWizard({
  tool,
  onClose,
}: {
  tool: CliTool | null;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const [step, setStep] = useState<Step>(1);
  const [draft, setDraft] = useState<CliTool | null>(tool);

  const ensurePersisted = async (partial: Partial<CliTool>): Promise<CliTool> => {
    if (draft?.id) {
      const updated = await cliToolsApi.update(draft.id, partial);
      setDraft(updated);
      return updated;
    }
    const created = await cliToolsApi.create({
      ...partial,
      config: defaultCliToolConfig(),
      parameters_schema: {},
    });
    setDraft(created);
    return created;
  };

  const labels = [
    t('enterprise.cliTools.wizard.stepBasic', 'Basic info'),
    t('enterprise.cliTools.wizard.stepBinary', 'Binary'),
    t('enterprise.cliTools.wizard.stepConfig', 'Configuration & test'),
  ];

  const title = draft?.display_name || t('enterprise.cliTools.addButton', 'Add CLI Tool');

  return (
    <div
      style={{
        position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
        background: 'rgba(0,0,0,0.55)',
        zIndex: 2000,
        display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: 'var(--bg-primary)',
          borderRadius: '12px',
          padding: '24px',
          width: '480px',
          maxWidth: '95vw',
          maxHeight: '80vh',
          overflow: 'auto',
          boxShadow: '0 20px 60px rgba(0,0,0,0.4)',
        }}
      >
        {/* Header */}
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
          <div>
            <h3 style={{ margin: 0 }}>🛠️ {title}</h3>
            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>
              {t('enterprise.cliTools.wizard.subtitle', 'Upload a binary, configure env, run a test')}
            </div>
          </div>
          <button
            onClick={onClose}
            style={{ background: 'none', border: 'none', fontSize: '18px', cursor: 'pointer', color: 'var(--text-secondary)' }}
          >
            ✕
          </button>
        </div>

        {/* Step indicator */}
        <div style={{ display: 'flex', gap: '4px', marginBottom: '16px', fontSize: '12px' }}>
          {labels.map((label, idx) => {
            const n = (idx + 1) as Step;
            const active = step === n;
            const done = step > n;
            return (
              <div
                key={label}
                style={{
                  flex: 1,
                  padding: '8px 6px',
                  textAlign: 'center',
                  borderRadius: '6px',
                  background: active
                    ? 'var(--accent-primary)'
                    : done ? 'var(--bg-tertiary)' : 'transparent',
                  color: active ? '#fff' : done ? 'var(--text-primary)' : 'var(--text-tertiary)',
                  fontWeight: active ? 600 : 400,
                  border: !active && !done ? '1px dashed var(--border-subtle)' : '1px solid transparent',
                  userSelect: 'none',
                }}
              >
                {n}. {label}
              </div>
            );
          })}
        </div>

        {/* Body */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
          {step === 1 && (
            <BasicInfoStep
              tool={draft}
              onNext={async (values) => {
                await ensurePersisted(values);
                setStep(2);
              }}
              onCancel={onClose}
            />
          )}
          {step === 2 && draft && (
            <BinaryStep
              tool={draft}
              onReplaced={(updated) => setDraft(updated)}
              onBack={() => setStep(1)}
              onNext={() => setStep(3)}
            />
          )}
          {step === 3 && draft && (
            <ConfigStep
              tool={draft}
              onUpdated={(updated) => setDraft(updated)}
              onBack={() => setStep(2)}
              onDone={onClose}
            />
          )}
        </div>
      </div>
    </div>
  );
}
