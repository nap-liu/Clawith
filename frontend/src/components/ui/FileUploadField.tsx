import { useRef } from 'react';
import { IconUpload } from '@tabler/icons-react';
import Button from './Button';
import './FileUploadField.css';

interface FileUploadFieldProps {
    label: string;
    accept: string;
    file: File | null;
    onChange: (file: File | null) => void;
    disabled?: boolean;
}

/** Shared accessible file picker using the standard button control. */
export default function FileUploadField({ label, accept, file, onChange, disabled }: FileUploadFieldProps) {
    const input = useRef<HTMLInputElement>(null);
    return <div className="ui-file-upload-field">
        <input ref={input} type="file" hidden accept={accept} disabled={disabled} aria-label={label}
            onChange={event => onChange(event.target.files?.[0] || null)} />
        <Button type="button" variant="secondary" disabled={disabled} onClick={() => input.current?.click()}>
            <IconUpload size={16} />{label}
        </Button>
        {file && <span title={file.name}>{file.name}</span>}
    </div>;
}
