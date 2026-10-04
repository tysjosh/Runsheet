/**
 * Shared parsing and state handling for the `/api/chat` event stream.
 *
 * The backend sends Server-Sent Events, one JSON object per `data:` line:
 * `status`, `tool`, `tool_result`, `text`, `error` and a final `done`.
 *
 * Both dispatcher chat clients (the AIChat drawer and the command page) used
 * to throw on `data.error` inside their per-line `try`, so the parse `catch`
 * swallowed it and an AI failure never reached the user (staging finding F3).
 * Parsing and the message reducer live here so the two clients cannot drift.
 */

export type ChatStreamRole =
  | "user"
  | "assistant"
  | "tool-indicator"
  | "confirmation";

/** The message fields the reducer reads and writes. */
export interface ChatStreamMessage {
  id: string;
  role: ChatStreamRole;
  content: string;
  timestamp: Date;
  isStreaming?: boolean;
  toolName?: string;
  toolStatus?: "in-progress" | "done";
  isContinuation?: boolean;
  /** True when the assistant turn ended in an AI-service error. */
  isError?: boolean;
}

export type ChatStreamEvent =
  | { type: "text"; content: string }
  | { type: "tool"; tool_name: string; tool_input?: unknown }
  | { type: "tool_result"; tool_name?: string; status?: "success" | "error" }
  | { type: "status"; stage: string; [key: string]: unknown }
  | {
      type: "error";
      code: string;
      message: string;
      request_id?: string | null;
      retry_after_seconds?: number;
    }
  | { type: "done" }
  | {
      type: "confirmation";
      action: {
        action_id?: string;
        tool_name?: string;
        risk_level?: string;
        summary?: string;
        impact_summary?: string;
      };
    };

const FALLBACK_ERROR_MESSAGE =
  "The AI assistant is temporarily unavailable. Please try again shortly.";

/**
 * Split a growing SSE buffer into complete events plus the unfinished tail.
 *
 * Feed it `buffer + decoder.decode(chunk, { stream: true })` and keep `rest`
 * for the next chunk. Blank lines and `:` comments are ignored; a malformed
 * line is skipped. The legacy `{"error": "..."}` shape becomes an `error`
 * event instead of being dropped.
 */
export function parseSseChunk(buffer: string): {
  events: ChatStreamEvent[];
  rest: string;
} {
  const lines = buffer.split("\n");
  const rest = lines.pop() ?? "";
  const events: ChatStreamEvent[] = [];

  for (const rawLine of lines) {
    const line = rawLine.replace(/\r$/, "");
    if (!line.startsWith("data:")) continue;
    const payload = line.slice(5).trim();
    if (!payload) continue;

    let data: unknown;
    try {
      data = JSON.parse(payload);
    } catch {
      console.warn("Skipping malformed chat stream line");
      continue;
    }
    if (!data || typeof data !== "object") continue;
    const obj = data as Record<string, unknown>;

    if (typeof obj.type === "string") {
      events.push(obj as ChatStreamEvent);
    } else if ("error" in obj) {
      events.push({
        type: "error",
        code: "AI_SERVICE_UNAVAILABLE",
        message:
          typeof obj.error === "string" && obj.error
            ? obj.error
            : FALLBACK_ERROR_MESSAGE,
      });
    }
  }

  return { events, rest };
}

let idSeq = 0;
const nextId = (prefix: string) => `${prefix}-${Date.now()}-${++idSeq}`;

function lastIndexWhere<M>(items: M[], pred: (m: M) => boolean): number {
  for (let i = items.length - 1; i >= 0; i--) {
    if (pred(items[i])) return i;
  }
  return -1;
}

/**
 * Apply one stream event to the message list. Pure: returns a new array and
 * never mutates the messages it was given. `status` and `confirmation`
 * events are left to the caller.
 */
export function applyChatStreamEvent<M extends ChatStreamMessage>(
  messages: M[],
  event: ChatStreamEvent,
): M[] {
  const streamingIdx = lastIndexWhere(
    messages,
    (m) => m.role === "assistant" && Boolean(m.isStreaming),
  );

  switch (event.type) {
    case "text": {
      if (!event.content || streamingIdx === -1) return messages;
      const updated = [...messages];
      const target = updated[streamingIdx];
      updated[streamingIdx] = {
        ...target,
        content: target.content + event.content,
      };
      return updated;
    }

    case "tool": {
      if (!event.tool_name) return messages;
      const lastAssistantIdx = lastIndexWhere(
        messages,
        (m) => m.role === "assistant",
      );
      if (lastAssistantIdx === -1 || !messages[lastAssistantIdx].isStreaming) {
        return messages;
      }
      const updated = [...messages];
      updated[lastAssistantIdx] = {
        ...updated[lastAssistantIdx],
        isStreaming: false,
      };
      updated.push(
        {
          id: nextId("tool"),
          role: "tool-indicator",
          content: "",
          timestamp: new Date(),
          toolName: event.tool_name,
          toolStatus: "in-progress",
        } as M,
        {
          id: nextId("assistant"),
          role: "assistant",
          content: "",
          timestamp: new Date(),
          isStreaming: true,
          isContinuation: true,
        } as M,
      );
      return updated;
    }

    case "tool_result": {
      const idx = messages.findIndex(
        (m) => m.role === "tool-indicator" && m.toolStatus === "in-progress",
      );
      if (idx === -1) return messages;
      const updated = [...messages];
      updated[idx] = { ...updated[idx], toolStatus: "done" };
      return updated;
    }

    case "error": {
      const reference = event.request_id
        ? `\n\nReference: ${event.request_id}`
        : "";
      const errorFields = {
        content: `${event.message || FALLBACK_ERROR_MESSAGE}${reference}`,
        isError: true,
        isStreaming: false,
      };
      const updated = [...messages];
      if (streamingIdx === -1) {
        updated.push({
          id: nextId("assistant"),
          role: "assistant",
          timestamp: new Date(),
          ...errorFields,
        } as M);
      } else {
        updated[streamingIdx] = { ...updated[streamingIdx], ...errorFields };
      }
      return updated;
    }

    case "done": {
      if (streamingIdx === -1) return messages;
      const updated = [...messages];
      updated[streamingIdx] = { ...updated[streamingIdx], isStreaming: false };
      return updated;
    }

    default:
      return messages;
  }
}
