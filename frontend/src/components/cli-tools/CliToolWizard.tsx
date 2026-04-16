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
      className="modal-overlay"
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(0,0,0,0.5)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        zIndex: 1000,
      }}
    >
      <div
        className="modal-content wide"
        style={{
          background: 'var(--bg-primary)',
          borderRadius: 8,
          width: 640,
          maxHeight: '90vh',
          overflow: 'auto',
        }}
      >
        <div
          className="wizard-tabs"
          style={{
            display: 'flex',
            borderBottom: '1px solid var(--border)',
          }}
        >
          {labels.map((label, idx) => {
            const n = (idx + 1) as Step;
            return (
              <div
                key={label}
                className={`wizard-tab ${step === n ? 'active' : ''}`}
                style={{
                  flex: 1,
                  padding: 12,
                  textAlign: 'center',
                  borderBottom: step === n ? '2px solid var(--accent-primary)' : 'none',
                  color: step === n ? 'var(--accent-primary)' : 'var(--text-secondary)',
                  fontWeight: step === n ? 600 : 400,
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
