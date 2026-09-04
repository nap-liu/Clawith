export type BackgroundResourceType = 'trigger' | 'task' | 'schedule';

export type BackgroundResourceTarget = {
    type: BackgroundResourceType;
    resource: any;
} | null;

export type ExecutionUserPickerTarget = {
    resourceType: BackgroundResourceType;
    resourceId: string;
    executionUserId: string;
    expectedExecutionUserId: string | null;
} | null;

export type Translate = (key: string, options?: Record<string, unknown> | string) => string;
