import {
    IconFileCode,
    IconFileMusic,
    IconFileTypeDocx,
    IconFileTypePdf,
    IconFileTypePpt,
    IconFileTypeTxt,
    IconFileTypeXls,
    IconFileTypeZip,
    IconFileUnknown,
    IconVideo,
} from '@tabler/icons-react';

import {
    getChatAttachmentIconKind,
    type ChatMessageAttachment,
} from '../utils/chatAttachments';
import './ChatAttachmentIcon.css';

const ICONS = {
    pdf: IconFileTypePdf,
    word: IconFileTypeDocx,
    spreadsheet: IconFileTypeXls,
    presentation: IconFileTypePpt,
    archive: IconFileTypeZip,
    text: IconFileTypeTxt,
    code: IconFileCode,
    audio: IconFileMusic,
    video: IconVideo,
    generic: IconFileUnknown,
};

export default function ChatAttachmentIcon({
    name,
    mimeType,
    kind,
    size = 16,
    stroke = 1.75,
}: {
    name: string;
    mimeType?: string;
    kind?: ChatMessageAttachment['kind'];
    size?: number;
    stroke?: number;
}) {
    const iconKind = getChatAttachmentIconKind({ name, mimeType, kind });
    const Icon = ICONS[iconKind];
    return (
        <Icon
            aria-hidden="true"
            className={`chat-attachment-type-icon chat-attachment-type-icon--${iconKind}`}
            size={size}
            stroke={stroke}
        />
    );
}
