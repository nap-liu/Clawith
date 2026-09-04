import AwareTabContent, { type AwareTabContentProps } from './AwareTabContent';

export default function AwarePreviewPanel(props: AwareTabContentProps) {
    return (
        <div className="aware-side-preview aware-side-preview--workspace">
            <AwareTabContent {...props} embedded />
        </div>
    );
}
