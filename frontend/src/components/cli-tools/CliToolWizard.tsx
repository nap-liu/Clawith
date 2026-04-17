import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { CliTool } from './types';
import { defaultRuntimeConfig, defaultSandboxConfig } from './types';
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

  // Basic-info submission carries only name / display_name / description.
  // Binary metadata is never created-or-edited here; it appears later
  // through the upload endpoint. Runtime + sandbox are seeded with the
  // admin-editable defaults so the post-create row is immediately valid.
  const ensurePersisted = async (partial: Partial<CliTool>): Promise<CliTool> => {
    if (draft?.id) {
      const updated = await cliToolsApi.update(draft.id, {
        display_name: partial.display_name,
        description: partial.description,
      });
      setDraft(updated);
      return updated;
    }
    if (!partial.name || !partial.display_name) {
      throw new Error('name and display_name are required to create a CLI tool');
    }
    const created = await cliToolsApi.create({
      name: partial.name,
      display_name: partial.display_name,
      description: partial.description ?? '',
      parameters_schema: {},
      runtime: defaultRuntimeConfig(),
      sandbox: defaultSandboxConfig(),
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

        {/* Step indicator — clickable once the draft has been persisted
            (i.e. opened in edit mode, or step 1 just submitted). Before
            that, jumping forward is meaningless because there's nothing
            to configure a binary/schema against. */}
        <div style={{ display: 'flex', gap: '4px', marginBottom: '16px', fontSize: '12px' }}>
          {labels.map((label, idx) => {
            const n = (idx + 1) as Step;
            const active = step === n;
            const done = step > n;
            const jumpable = !!draft?.id;
            return (
              <div
                key={label}
                onClick={jumpable && !active ? () => setStep(n) : undefined}
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
                  cursor: jumpable && !active ? 'pointer' : 'default',
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
