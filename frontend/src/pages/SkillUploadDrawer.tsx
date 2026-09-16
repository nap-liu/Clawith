import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { IconUpload, IconX } from '@tabler/icons-react';
import { Drawer } from '../components/Dialog/DialogProvider';
import { useToast } from '../components/Toast/ToastProvider';
import Button from '../components/ui/Button';
import TextInput from '../components/ui/TextInput';
import FileUploadField from '../components/ui/FileUploadField';
import SelectDropdown from '../components/SelectDropdown';
import { skillApi } from '../services/api';

export default function SkillUploadDrawer({ platformAdmin, onClose, onUploaded }: {
    platformAdmin: boolean; onClose: () => void; onUploaded: () => void;
}) {
    const { t } = useTranslation();
    const toast = useToast();
    const [file, setFile] = useState<File | null>(null);
    const [folder, setFolder] = useState('');
    const [scope, setScope] = useState(platformAdmin ? 'platform' : 'tenant');
    const [busy, setBusy] = useState(false);
    const upload = async (event: React.FormEvent) => {
        event.preventDefault();
        if (!file) return;
        setBusy(true);
        try {
            await skillApi.manage.upload(file, folder.trim(), scope);
            onUploaded();
            toast.success(t('skillManagement.uploaded'));
            onClose();
        } catch (error) { toast.error(t('skillManagement.failed'), { details: String(error) }); }
        finally { setBusy(false); }
    };
    return <Drawer open onClose={() => { if (!busy) onClose(); }} className="skill-form-drawer" ariaLabelledBy="skill-upload-title" closeOnEscape={!busy} closeOnBackdrop={!busy}>
        <header className="skill-preview-header">
            <div className="skill-preview-heading"><h2 id="skill-upload-title">{t('skillManagement.upload')}</h2></div>
            <Button variant="ghost" onClick={onClose} disabled={busy} aria-label={t('common.close')}><IconX size={18} /></Button>
        </header>
        <form className="skill-drawer-form" onSubmit={upload}>
            <div className="skill-market-form-body">
                <p className="skill-market-form-note">{t('skillManagement.uploadHint')}</p>
                <FileUploadField label={t('skillManagement.chooseFile')} accept=".zip,.md" file={file} disabled={busy} onChange={next => {
                    setFile(next);
                    setFolder(next && !/^skill\.md$/i.test(next.name) ? next.name.replace(/\.(zip|md)$/i, '').replace(/[^a-zA-Z0-9_-]/g, '-') : '');
                }} />
                <label>{t('skillMarket.skillFolder')}<TextInput value={folder} onChange={event => setFolder(event.target.value)} required maxLength={100} pattern="[a-zA-Z0-9][a-zA-Z0-9_-]*" disabled={busy} /></label>
                <p className="skill-market-form-note">{t('skillManagement.folderHint')}</p>
                {platformAdmin && <label>{t('skillManagement.uploadScope')}<SelectDropdown value={scope} onChange={setScope} ariaLabel={t('skillManagement.uploadScope')}
                    options={[{ value: 'platform', label: t('skillMarket.platformPublic') }, { value: 'tenant', label: t('skillMarket.companyOnly') }]} /></label>}
            </div>
            <footer className="skill-market-install-actions">
                <Button variant="secondary" onClick={onClose} disabled={busy} type="button">{t('common.cancel')}</Button>
                <Button variant="primary" type="submit" disabled={busy || !file || !folder.trim()}><IconUpload size={16} />{t(busy ? 'common.loading' : 'skillManagement.upload')}</Button>
            </footer>
        </form>
    </Drawer>;
}
