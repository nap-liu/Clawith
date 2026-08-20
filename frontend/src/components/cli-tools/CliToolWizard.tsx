import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { IconX } from '@tabler/icons-react';
import { Modal } from '../Dialog/DialogProvider';
import Button from '../ui/Button';
import type { CliTool } from './types';
import { cliToolsApi } from './api';
import { BasicInfoStep } from './steps/BasicInfoStep';
import { BinaryStep } from './steps/BinaryStep';
import { ConfigStep } from './steps/ConfigStep';

type Step = 1 | 2 | 3;

export function CliToolWizard({
  open,
  tool,
  onClose,
}: {
  open: boolean;
  tool: CliTool | null;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const [step, setStep] = useState<Step>(1);
  const [draft, setDraft] = useState<CliTool | null>(tool);

  useEffect(() => {
    if (!open) return;
    setStep(1);
    setDraft(tool);
  }, [open, tool]);

  // Basic-info submission carries only name / display_name / description.
  // Binary metadata is managed separately through the upload endpoint.
  // Env injection is configured in the final step.
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
    });
    setDraft(created);
    return created;
  };

  const labels = [
    t('enterprise.cliTools.wizard.stepBasic', 'Basic info'),
    t('enterprise.cliTools.wizard.stepBinary', 'Binary'),
    t('enterprise.cliTools.wizard.stepConfig', 'Env & test'),
  ];

  const title = draft?.display_name || t('enterprise.cliTools.addButton', 'Add CLI Tool');

  return (
    <Modal
      open={open}
      onClose={onClose}
      ariaLabelledBy="cli-tool-wizard-title"
      className="cli-tool-wizard-modal"
      style={{ width: 'min(480px, calc(100vw - 40px))' }}
    >
      <div
        style={{
          padding: '24px',
        }}
      >
        {/* Header */}
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
          <div>
            <h3 id="cli-tool-wizard-title" style={{ margin: 0 }}>🛠️ {title}</h3>
            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>
              {t('enterprise.cliTools.wizard.subtitle', 'Upload a binary, configure env, run a test')}
            </div>
          </div>
          <Button
            type="button"
            variant="ghost"
            onClick={onClose}
            aria-label={t('common.close')}
            style={{ minWidth: '34px', padding: '7px' }}
          >
            <IconX size={18} />
          </Button>
        </div>

        {/* Step indicator — clickable once the draft has been persisted
            (i.e. opened in edit mode, or step 1 just submitted). Before
            that, jumping forward is meaningless because there's nothing
            to configure a binary/env against. */}
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
    </Modal>
  );
}
