import H5AgentChatView from './H5AgentChat/H5AgentChatView';
import { useH5AgentChatComposer } from './H5AgentChat/useH5AgentChatComposer';
import { useH5AgentChatHistory } from './H5AgentChat/useH5AgentChatHistory';
import { useH5AgentChatLifecycle } from './H5AgentChat/useH5AgentChatLifecycle';
import { useH5AgentChatPresentation } from './H5AgentChat/useH5AgentChatPresentation';
import { useH5AgentChatSocket } from './H5AgentChat/useH5AgentChatSocket';
import { useH5AgentChatState } from './H5AgentChat/useH5AgentChatState';
import './H5AgentChat.css';

export { buildCodeExchangeRedirectUri } from './H5AgentChat/model';

export default function H5AgentChat() {
    const state = useH5AgentChatState();
    const lifecycle = useH5AgentChatLifecycle(state);
    const history = useH5AgentChatHistory(state);
    const socket = useH5AgentChatSocket(state, history, lifecycle);
    const composer = useH5AgentChatComposer(state, history, socket);
    const presentation = useH5AgentChatPresentation(
        state,
        history,
        lifecycle,
        socket,
        composer,
    );

    return (
        <H5AgentChatView
            state={state}
            history={history}
            lifecycle={lifecycle}
            socket={socket}
            composer={composer}
            presentation={presentation}
        />
    );
}
