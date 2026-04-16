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

  // Create flow: persist metadata at end of step 1, then step 2 operates on a real tool_id.
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

  return (
    <div
      style={{
        position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
        background: 'rgba(0,0,0,0.5)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        zIndex: 10000,
      }}
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div
        style={{
          background: 'var(--bg-primary)',
          borderRadius: '12px',
          width: '640px',
          maxWidth: '90vw',
          maxHeight: '90vh',
          overflow: 'auto',
          border: '1px solid var(--border-subtle)',
          boxShadow: '0 20px 60px rgba(0,0,0,0.4)',
        }}
      >
        <div
          style={{
            display: 'flex',
            borderBottom: '1px solid var(--border-subtle)',
          }}
        >
          {labels.map((label, idx) => {
            const n = (idx + 1) as Step;
            const active = step === n;
            return (
              <div
                key={label}
                style={{
                  flex: 1,
                  padding: '14px 12px',
                  textAlign: 'center',
                  fontSize: '13px',
                  borderBottom: active ? '2px solid var(--accent-primary)' : '2px solid transparent',
                  color: active ? 'var(--accent-primary)' : 'var(--text-secondary)',
                  fontWeight: active ? 600 : 400,
                }}
              >
                {n}. {label}
              </div>
            );
          })}
        </div>

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
  );
}
