import { createServer } from 'vite';
import { runH5ChatTimelinePart1 } from './h5-chat-timeline/part1.mjs';
import { runH5ChatTimelinePart2 } from './h5-chat-timeline/part2.mjs';
import { runH5ChatTimelinePart3 } from './h5-chat-timeline/part3.mjs';
import { runH5ChatTimelinePart4 } from './h5-chat-timeline/part4.mjs';

const server = await createServer({
    appType: 'custom',
    optimizeDeps: { noDiscovery: true },
    server: { middlewareMode: true },
});

try {
    // Load the production dependency graph, including ChatToolCallRenderer.
    // Web and the H5 compatibility facade consume this same conversation core.
    const [timeline, lifecycle, resumeRecovery, fileDelivery] = await Promise.all([
        server.ssrLoadModule('/src/features/conversation/core/chatTimeline.ts'),
        server.ssrLoadModule('/src/features/conversation/core/conversationTurnLifecycle.ts'),
        server.ssrLoadModule('/src/features/conversation/core/resumeRecovery.ts'),
        server.ssrLoadModule('/src/utils/chatFileDelivery.ts'),
    ]);
    const testContext = {
        ...timeline,
        ...lifecycle,
        ...resumeRecovery,
        ...fileDelivery,
        buildH5ConversationEntries: timeline.buildConversationEntries,
        getH5ScrollAnchor: timeline.getConversationScrollAnchor,
    };

    runH5ChatTimelinePart1(testContext);
    runH5ChatTimelinePart2(testContext);
    runH5ChatTimelinePart3(testContext);
    runH5ChatTimelinePart4(testContext);

    console.log('h5 chat timeline tests passed');
} finally {
    await server.close();
}
