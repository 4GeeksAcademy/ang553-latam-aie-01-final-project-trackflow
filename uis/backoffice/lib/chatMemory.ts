import type { SupportMemoryProposal } from "../types/support";

const CONVERSATION_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function chatMemoryConversationId(sessionId: string): string | null {
  // Legacy chat IDs use the same UUID bytes as the server's memory binding.
  const legacy = /^chat_([0-9a-f]{8})([0-9a-f]{4})([0-9a-f]{4})([0-9a-f]{4})([0-9a-f]{12})$/i.exec(sessionId);
  const uuid = (legacy ? legacy.slice(1).join("-") : sessionId).toLowerCase();
  return CONVERSATION_ID.test(uuid) ? uuid : null;
}

export function currentChatMemoryProposal(
  proposal: SupportMemoryProposal | null,
  conversationId: string,
  clientId: string,
  now = Date.now(),
): SupportMemoryProposal | null {
  return proposal?.conversation_id === conversationId &&
    proposal.scope === "client" &&
    proposal.client_id === clientId &&
    Date.parse(proposal.expires_at) > now
    ? proposal
    : null;
}
