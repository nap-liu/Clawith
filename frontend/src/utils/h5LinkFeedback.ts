type CopyH5LinkWithFeedbackOptions = {
    copy: (url: string) => Promise<boolean>;
    onCopied: () => void;
    onCopyFailed: () => void;
};

export async function copyH5LinkWithFeedback(
    url: string,
    options: CopyH5LinkWithFeedbackOptions,
): Promise<boolean> {
    let copied = false;
    try {
        copied = await options.copy(url);
    } catch {
        copied = false;
    }

    if (copied) {
        options.onCopied();
    } else {
        options.onCopyFailed();
    }
    return copied;
}
