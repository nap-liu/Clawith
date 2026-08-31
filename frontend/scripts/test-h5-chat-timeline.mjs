import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { existsSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import vm from 'node:vm';
import { runH5ChatTimelinePart1 } from './h5-chat-timeline/part1.mjs';
import { runH5ChatTimelinePart2 } from './h5-chat-timeline/part2.mjs';
import { runH5ChatTimelinePart3 } from './h5-chat-timeline/part3.mjs';
import { runH5ChatTimelinePart4 } from './h5-chat-timeline/part4.mjs';

const require = createRequire(import.meta.url);
const ts = require('typescript');

const __dirname = dirname(fileURLToPath(import.meta.url));
const compiledModuleCache = new Map();
const fileDeliveryPath = resolve(__dirname, '../src/utils/chatFileDelivery.ts');
const chatAttachmentsPath = resolve(__dirname, '../src/utils/chatAttachments.ts');
const clientIdPath = resolve(__dirname, '../src/utils/clientId.ts');
const chatToolCallRendererPath = resolve(__dirname, '../src/components/ChatToolCallRenderer.tsx');

function resolveSpecialModule(id, resolvedPath) {
    if (id === '../../../utils/chatFileDelivery' || resolvedPath === fileDeliveryPath) {
        return fileDeliveryModule.exports;
    }
    if (id === '../../../utils/chatAttachments' || resolvedPath === chatAttachmentsPath) {
        return chatAttachmentsModule.exports;
    }
    if (id === '../../../utils/clientId' || resolvedPath === clientIdPath) {
        return { createClientId: () => 'test-client-id' };
    }
    if (id === '../../../components/ChatToolCallRenderer' || resolvedPath === chatToolCallRendererPath) {
        return {
            getChatToolRenderType: (message) => (
                message?.role !== 'tool_call'
                    ? null
                    : message?.toolName === 'request_confirmation'
                        ? 'confirmation'
                        : message?.toolName === 'send_channel_file'
                            ? 'file-delivery'
                            : (parseFileDeliveryToolResult(
                                message?.toolName,
                                message?.toolResult,
                                message?.toolArgs,
                                message?.toolCallId,
                            )?.mediaKind || parseMediaDeliveryErrorResult(
                                message?.toolName,
                                message?.toolResult,
                            ))
                                ? 'media-delivery'
                                : null
            ),
            getChatToolRenderIdentity: (message) => {
                if (message?.role !== 'tool_call' || message?.toolName !== 'send_channel_file') return null;
                try {
                    const delivery = JSON.parse(message.toolResult || '{}');
                    return ['file-delivery', delivery.path, delivery.filename, delivery.message || ''].join(':');
                } catch {
                    return null;
                }
            },
        };
    }
    return null;
}

function resolveTsDependencyPath(importerPath, id) {
    const basePath = resolve(dirname(importerPath), id);
    const candidates = [
        basePath,
        `${basePath}.ts`,
        `${basePath}.tsx`,
        resolve(basePath, 'index.ts'),
        resolve(basePath, 'index.tsx'),
    ];
    return candidates.find((candidate) => existsSync(candidate)) || null;
}

function makeTsRequire(sourcePath, requireOverride = require) {
    return (id) => {
        if (id.startsWith('.')) {
            const dependencyPath = resolveTsDependencyPath(sourcePath, id);
            if (!dependencyPath) {
                throw new Error(`Cannot resolve module '${id}' from ${sourcePath}`);
            }
            const specialModule = resolveSpecialModule(id, dependencyPath);
            if (specialModule) return specialModule;
            return compileTsModule(dependencyPath, requireOverride).exports;
        }
        return requireOverride(id);
    };
}

function compileTsModule(sourcePath, requireOverride = require) {
    const cachedModule = compiledModuleCache.get(sourcePath);
    if (cachedModule) return cachedModule;
    const source = readFileSync(sourcePath, 'utf8');
    const compiled = ts.transpileModule(source, {
        compilerOptions: {
            module: ts.ModuleKind.CommonJS,
            target: ts.ScriptTarget.ES2020,
            esModuleInterop: true,
            jsx: ts.JsxEmit.ReactJSX,
        },
    }).outputText;

    const localModule = { exports: {} };
    compiledModuleCache.set(sourcePath, localModule);
    vm.runInNewContext(compiled, {
        module: localModule,
        exports: localModule.exports,
        require: makeTsRequire(sourcePath, requireOverride),
        console,
        URL,
    }, { filename: sourcePath });
    return localModule;
}

const fileDeliveryModule = compileTsModule(fileDeliveryPath);
const { parseFileDeliveryToolResult, parseMediaDeliveryErrorResult } = fileDeliveryModule.exports;
const chatAttachmentsModule = compileTsModule(chatAttachmentsPath);

// Compile the canonical shared conversation core directly. The H5 module is a
// compatibility facade over this file, so these fixtures lock the behaviour H5
// consumes while allowing Web to share the same transformations.
const sourcePath = resolve(__dirname, '../src/features/conversation/core/chatTimeline.ts');
const timelineRequire = (id) => {
    const specialModule = resolveSpecialModule(id, null);
    if (specialModule) return specialModule;
    return require(id);
};

const module = compileTsModule(sourcePath, timelineRequire);
const lifecycleModule = compileTsModule(resolve(
    __dirname,
    '../src/features/conversation/core/conversationTurnLifecycle.ts',
));
const resumeRecoveryModule = compileTsModule(resolve(
    __dirname,
    '../src/features/conversation/core/resumeRecovery.ts',
));

const {
    applyAssistantDoneMessage,
    applyAssistantMessageCommitted,
    applyAssistantStreamMessage,
    applyConfirmationRequiredEvent,
    applyUserMessageCommitted,
    foldConversationTimelineEvent,
    buildConversationEntries: buildH5ConversationEntries,
    getConversationScrollAnchor: getH5ScrollAnchor,
    hasPendingConfirmation,
    isA2AMessageLeft,
    isConfirmationToolCall,
    latestHistoryWindowOverlaps,
    mapHistoryMessage,
    mergeHistoryMessages,
    normalizeChatTimelineMessages,
    projectConversationTurnProgress,
    reconcileLatestHistoryWindow,
    shouldProjectConversationTurnProgress,
    toolCallMessageFromEvent,
    upsertToolCallMessage,
} = module.exports;
const {
    bufferResumeEvent,
    createResumeEventGate,
    createOrReuseResumeEventGate,
    drainResumeEventGate,
    prepareMessagesForActiveTurnResume,
    shouldScheduleResumeReconnect,
} = resumeRecoveryModule.exports;
const {
    IDLE_CONVERSATION_TURN,
    beginConversationTurnRecovery,
    conversationTurnIsWaiting,
    conversationTurnEventClosesStream,
    conversationTurnEventShouldBeHandled,
    reduceConversationTurnEvent,
} = lifecycleModule.exports;

const testContext = {
    applyAssistantDoneMessage,
    applyAssistantMessageCommitted,
    applyAssistantStreamMessage,
    applyConfirmationRequiredEvent,
    applyUserMessageCommitted,
    foldConversationTimelineEvent,
    buildH5ConversationEntries,
    getH5ScrollAnchor,
    hasPendingConfirmation,
    isA2AMessageLeft,
    isConfirmationToolCall,
    latestHistoryWindowOverlaps,
    mapHistoryMessage,
    mergeHistoryMessages,
    normalizeChatTimelineMessages,
    projectConversationTurnProgress,
    reconcileLatestHistoryWindow,
    shouldProjectConversationTurnProgress,
    toolCallMessageFromEvent,
    upsertToolCallMessage,
    bufferResumeEvent,
    createResumeEventGate,
    createOrReuseResumeEventGate,
    drainResumeEventGate,
    prepareMessagesForActiveTurnResume,
    shouldScheduleResumeReconnect,
    IDLE_CONVERSATION_TURN,
    beginConversationTurnRecovery,
    conversationTurnIsWaiting,
    conversationTurnEventClosesStream,
    conversationTurnEventShouldBeHandled,
    reduceConversationTurnEvent,
    parseFileDeliveryToolResult,
    parseMediaDeliveryErrorResult,
};

runH5ChatTimelinePart1(testContext);
runH5ChatTimelinePart2(testContext);
runH5ChatTimelinePart3(testContext);
runH5ChatTimelinePart4(testContext);

console.log('h5 chat timeline tests passed');
