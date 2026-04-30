"use client";

/**
 * Command Interface page — the agent-first primary view.
 *
 * Features the AI chat prominently as the primary interaction surface
 * with a collapsible dashboard sidebar containing the Agent Activity Feed,
 * Approval Queue, and Agent Health panels. Supports inline action
 * confirmation for medium-risk actions within the chat flow.
 *
 * Validates:
 * - Requirement 9.1: Full-width command interface with AI chat as primary view
 * - Requirement 9.4: Inline action confirmation for medium-risk actions
 */

import {
  ChevronLeft,
  ChevronRight,
  SendHorizontal,
  Terminal,
  Trash2,
} from "lucide-react";
import type React from "react";
import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import AgentActivityFeed from "../../../components/ops/AgentActivityFeed";
import AgentHealth from "../../../components/ops/AgentHealth";
import AgentToast from "../../../components/ops/AgentToast";
import ApprovalQueue from "../../../components/ops/ApprovalQueue";
import ReportViewer from "../../../components/ReportViewer";

// ─── Types ───────────────────────────────────────────────────────────────────

interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "tool-indicator" | "confirmation";
  content: string;
  timestamp: Date;
  isStreaming?: boolean;
  toolName?: string;
  toolStatus?: "in-progress" | "done";
  isContinuation?: boolean;
  /** For inline confirmation messages */
  confirmationData?: {
    actionId: string;
    toolName: string;
    riskLevel: string;
    summary: string;
    resolved?: boolean;
    decision?: "approved" | "rejected";
  };
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

const scrollbarStyles = `
  .custom-scrollbar::-webkit-scrollbar {
    width: 6px;
  }
  .custom-scrollbar::-webkit-scrollbar-track {
    background: transparent;
  }
  .custom-scrollbar::-webkit-scrollbar-thumb {
    background-color: #d1d5db;
    border-radius: 3px;
  }
  .custom-scrollbar::-webkit-scrollbar-thumb:hover {
    background-color: #9ca3af;
  }
  @keyframes slide-in-right {
    from { transform: translateX(100%); opacity: 0; }
    to { transform: translateX(0); opacity: 1; }
  }
  .animate-slide-in-right {
    animation: slide-in-right 0.3s ease-out;
  }
`;

function getToolIcon(toolName?: string): string {
  if (!toolName) return "🔧";
  if (toolName.startsWith("search_")) return "🔍";
  if (toolName.startsWith("generate_")) return "📊";
  if (toolName.includes("assign")) return "🔗";
  if (toolName.includes("cancel")) return "🚫";
  if (toolName.includes("reassign")) return "🔄";
  if (toolName.includes("fuel") || toolName.includes("refill")) return "⛽";
  if (toolName.includes("escalate")) return "⚠️";
  if (toolName.includes("status")) return "📋";
  if (toolName.includes("job") || toolName.includes("create")) return "📦";
  return "🔧";
}

function isReport(content: string): boolean {
  return (
    content.includes("# 📋 Operations Report") ||
    content.includes("# 📊 Performance Analysis Report") ||
    content.includes("# 🔍 Incident Analysis Report") ||
    (content.includes("Generated:") && content.includes("##"))
  );
}

// ─── Component ───────────────────────────────────────────────────────────────

export default function CommandInterfacePage() {
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      id: "1",
      role: "assistant",
      content:
        "Welcome to the Command Interface. I can execute operations, manage fleet assets, handle scheduling, monitor fuel levels, and more. What would you like to do?",
      timestamp: new Date(),
    },
  ]);
  const [input, setInput] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);
  const [reportContent, setReportContent] = useState<string | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const processingRef = useRef(false);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

  useEffect(() => {
    if (inputRef.current) {
      inputRef.current.focus();
    }
  }, []);

  // ─── Inline confirmation handler (Requirement 9.4) ──────────────────────

  const handleInlineConfirm = (messageId: string, decision: "approved" | "rejected") => {
    setMessages((prev) =>
      prev.map((msg) => {
        if (msg.id === messageId && msg.confirmationData) {
          return {
            ...msg,
            confirmationData: {
              ...msg.confirmationData,
              resolved: true,
              decision,
            },
          };
        }
        return msg;
      }),
    );

    // Add a follow-up assistant message
    const decisionText =
      decision === "approved"
        ? "Action approved. Executing now..."
        : "Action rejected. The operation has been cancelled.";

    setMessages((prev) => [
      ...prev,
      {
        id: `confirm-result-${Date.now()}`,
        role: "assistant",
        content: decisionText,
        timestamp: new Date(),
      },
    ]);
  };

  // ─── Chat streaming ─────────────────────────────────────────────────────

  const streamChatResponse = async (userMessage: string) => {
    const AI_STREAMING_TIMEOUT = 120000;
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), AI_STREAMING_TIMEOUT);

    try {
      const API_BASE_URL =
        process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api";
      const response = await fetch(`${API_BASE_URL}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: userMessage, mode: "chat" }),
        signal: controller.signal,
      });

      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }

      const reader = response.body?.getReader();
      if (!reader) throw new Error("No response body reader available");

      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        const chunk = decoder.decode(value, { stream: true });
        buffer += chunk;
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";

        for (const line of lines) {
          if (line.startsWith("data: ")) {
            try {
              const jsonStr = line.slice(6).trim();
              if (!jsonStr) continue;
              const data = JSON.parse(jsonStr);

              if (data.error) throw new Error(data.error);

              if (data.type === "text" && data.content) {
                setMessages((prev) => {
                  const updated = [...prev];
                  const lastIdx = updated.findLastIndex(
                    (msg) => msg.role === "assistant" && msg.isStreaming,
                  );
                  if (lastIdx !== -1) {
                    updated[lastIdx].content += data.content;
                  }
                  return updated;
                });
              }

              if (data.type === "tool" && data.tool_name) {
                setMessages((prev) => {
                  const updated = [...prev];
                  const lastIdx = updated.findLastIndex(
                    (msg) => msg.role === "assistant",
                  );
                  if (lastIdx !== -1 && updated[lastIdx].isStreaming) {
                    updated[lastIdx].isStreaming = false;
                    updated.push({
                      id: `tool-${Date.now()}`,
                      role: "tool-indicator",
                      content: "",
                      timestamp: new Date(),
                      toolName: data.tool_name,
                      toolStatus: "in-progress",
                    });
                    updated.push({
                      id: `assistant-${Date.now()}`,
                      role: "assistant",
                      content: "",
                      timestamp: new Date(),
                      isStreaming: true,
                      isContinuation: true,
                    });
                  }
                  return updated;
                });
              }

              // Inline confirmation for medium-risk actions (Requirement 9.4)
              if (data.type === "confirmation" && data.action) {
                setMessages((prev) => [
                  ...prev,
                  {
                    id: `confirm-${Date.now()}`,
                    role: "confirmation",
                    content: "",
                    timestamp: new Date(),
                    confirmationData: {
                      actionId: data.action.action_id || "",
                      toolName: data.action.tool_name || "",
                      riskLevel: data.action.risk_level || "medium",
                      summary: data.action.summary || data.action.impact_summary || "",
                    },
                  },
                ]);
              }

              if (data.type === "tool_result") {
                setMessages((prev) => {
                  const updated = [...prev];
                  const toolIdx = updated.findIndex(
                    (msg) =>
                      msg.role === "tool-indicator" &&
                      msg.toolStatus === "in-progress",
                  );
                  if (toolIdx !== -1) {
                    updated[toolIdx].toolStatus = "done";
                    setTimeout(() => {
                      setMessages((prevMsgs) =>
                        prevMsgs.filter(
                          (msg) => msg.id !== updated[toolIdx].id,
                        ),
                      );
                    }, 500);
                  }
                  return updated;
                });
              }

              if (data.type === "done") return;
            } catch (parseError) {
              console.warn("Failed to parse streaming data:", parseError);
            }
          }
        }
      }
    } catch (error) {
      console.error("Chat streaming error:", error);
      setMessages((prev) => {
        const updated = [...prev];
        const lastMsg = updated[updated.length - 1];
        if (lastMsg.role === "assistant") {
          if (error instanceof Error && error.name === "AbortError") {
            lastMsg.content =
              "⏱️ The request timed out after 120 seconds. Please try again with a simpler query.";
          } else {
            lastMsg.content = `❌ Sorry, I encountered an error connecting to the AI service. Please make sure the backend is running.\n\nError: ${error instanceof Error ? error.message : "Unknown error"}`;
          }
        }
        return updated;
      });
    } finally {
      clearTimeout(timeoutId);
    }
  };

  const handleSend = async () => {
    if (!input.trim() || isStreaming || processingRef.current) return;

    processingRef.current = true;
    setIsStreaming(true);

    const userMessage: ChatMessage = {
      id: Date.now().toString(),
      role: "user",
      content: input,
      timestamp: new Date(),
    };

    setMessages((prev) => [...prev, userMessage]);
    setInput("");

    const assistantMessage: ChatMessage = {
      id: (Date.now() + 1).toString(),
      role: "assistant",
      content: "",
      timestamp: new Date(),
      isStreaming: true,
    };

    setMessages((prev) => [...prev, assistantMessage]);

    try {
      await streamChatResponse(userMessage.content);
    } catch (error) {
      console.error("Chat error:", error);
      setMessages((prev) => {
        const updated = [...prev];
        const lastMsg = updated[updated.length - 1];
        if (lastMsg.role === "assistant") {
          lastMsg.content = "❌ Sorry, I encountered an error. Please try again.";
        }
        return updated;
      });
    } finally {
      setIsStreaming(false);
      setMessages((prev) => {
        const updated = [...prev];
        const lastMsg = updated[updated.length - 1];
        if (lastMsg.role === "assistant") {
          lastMsg.isStreaming = false;
        }
        return updated;
      });
      processingRef.current = false;
    }
  };

  const handleKeyPress = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const clearChat = async () => {
    try {
      const API_BASE_URL =
        process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api";
      await fetch(`${API_BASE_URL}/chat/clear`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
    } catch (error) {
      console.error("Error clearing chat:", error);
    }
    setMessages([
      {
        id: "1",
        role: "assistant",
        content: "Chat cleared. How can I help you with your operations?",
        timestamp: new Date(),
      },
    ]);
  };

  return (
    <>
      <style dangerouslySetInnerHTML={{ __html: scrollbarStyles }} />
      <div className="h-screen flex bg-gray-50">
        {/* ─── Main Chat Area ─────────────────────────────────────────── */}
        <div className="flex-1 flex flex-col min-w-0">
          {/* Header */}
          <div className="bg-white border-b border-gray-200/50 px-6 py-4 flex items-center justify-between flex-shrink-0">
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 bg-[#232323] rounded-xl flex items-center justify-center">
                <Terminal className="w-5 h-5 text-white" />
              </div>
              <div>
                <h1 className="text-lg font-semibold text-[#232323]">
                  Command Interface
                </h1>
                <p className="text-xs text-gray-500">
                  AI-powered operations control
                </p>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={clearChat}
                className="text-gray-400 hover:text-red-500 p-2 rounded-lg hover:bg-red-50 transition-all"
                title="Clear chat"
              >
                <Trash2 className="w-4 h-4" />
              </button>
              <button
                onClick={() => setSidebarOpen(!sidebarOpen)}
                className="text-gray-400 hover:text-[#232323] p-2 rounded-lg hover:bg-gray-100 transition-all"
                title={sidebarOpen ? "Hide sidebar" : "Show sidebar"}
              >
                {sidebarOpen ? (
                  <ChevronRight className="w-4 h-4" />
                ) : (
                  <ChevronLeft className="w-4 h-4" />
                )}
              </button>
            </div>
          </div>

          {/* Messages */}
          <div
            className="flex-1 overflow-y-auto px-6 py-4 space-y-4 custom-scrollbar"
            style={{
              scrollbarWidth: "thin",
              scrollbarColor: "#d1d5db transparent",
            }}
          >
            {messages.map((msg) => (
              <div
                key={msg.id}
                className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}
              >
                {msg.role === "tool-indicator" ? (
                  <div className="max-w-[85%] my-1">
                    <span
                      className="inline-block px-2 py-1 text-xs text-white rounded border"
                      style={{ backgroundColor: "#232323", borderColor: "#232323" }}
                    >
                      {getToolIcon(msg.toolName)} {msg.toolName || "tool"}
                    </span>
                  </div>
                ) : msg.role === "confirmation" && msg.confirmationData ? (
                  /* Inline action confirmation (Requirement 9.4) */
                  <div className="max-w-md w-full">
                    <div
                      className={`border rounded-xl p-4 ${
                        msg.confirmationData.resolved
                          ? msg.confirmationData.decision === "approved"
                            ? "border-emerald-200 bg-emerald-50/50"
                            : "border-gray-200 bg-gray-50/50"
                          : "border-amber-200 bg-amber-50/50"
                      }`}
                    >
                      <div className="flex items-center gap-2 mb-2">
                        <span className="text-xs font-semibold text-amber-700">
                          ⚡ Action Confirmation
                        </span>
                        <span
                          className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${
                            msg.confirmationData.riskLevel === "high"
                              ? "bg-red-100 text-red-600"
                              : "bg-amber-100 text-amber-600"
                          }`}
                        >
                          {msg.confirmationData.riskLevel} risk
                        </span>
                      </div>
                      <p className="text-xs font-medium text-gray-700 mb-1">
                        {msg.confirmationData.toolName}
                      </p>
                      <p className="text-xs text-gray-500 mb-3">
                        {msg.confirmationData.summary}
                      </p>
                      {!msg.confirmationData.resolved ? (
                        <div className="flex gap-2">
                          <button
                            onClick={() => handleInlineConfirm(msg.id, "approved")}
                            className="flex-1 px-3 py-1.5 text-xs font-medium text-white bg-emerald-600 hover:bg-emerald-700 rounded-lg transition-colors"
                          >
                            ✓ Approve
                          </button>
                          <button
                            onClick={() => handleInlineConfirm(msg.id, "rejected")}
                            className="flex-1 px-3 py-1.5 text-xs font-medium text-gray-600 bg-gray-100 hover:bg-gray-200 rounded-lg transition-colors"
                          >
                            ✕ Reject
                          </button>
                        </div>
                      ) : (
                        <p
                          className={`text-xs font-medium ${
                            msg.confirmationData.decision === "approved"
                              ? "text-emerald-600"
                              : "text-gray-500"
                          }`}
                        >
                          {msg.confirmationData.decision === "approved"
                            ? "✓ Approved"
                            : "✕ Rejected"}
                        </p>
                      )}
                    </div>
                  </div>
                ) : msg.role === "assistant" ? (
                  <div className="max-w-[85%]">
                    <div className="text-sm text-gray-800 leading-relaxed prose prose-sm max-w-none prose-headings:text-gray-900 prose-p:text-gray-800 prose-strong:text-gray-900 prose-ul:text-gray-800 prose-li:text-gray-800">
                      <ReactMarkdown>{msg.content}</ReactMarkdown>
                      {msg.isStreaming && (
                        <span
                          className="inline-block w-1.5 h-4 ml-1 animate-pulse rounded"
                          style={{ backgroundColor: "#232323" }}
                        />
                      )}
                    </div>
                    {isReport(msg.content) && !msg.isStreaming && (
                      <div className="mt-3 flex gap-2">
                        <button
                          onClick={() => setReportContent(msg.content)}
                          className="px-3 py-1.5 text-xs bg-blue-600 hover:bg-blue-700 text-white rounded-lg transition-colors flex items-center gap-1"
                        >
                          📊 View Report
                        </button>
                      </div>
                    )}
                  </div>
                ) : (
                  <div className="max-w-[75%]">
                    <div
                      className="text-white rounded-2xl px-4 py-2.5 shadow-lg"
                      style={{ backgroundColor: "#232323" }}
                    >
                      <div className="whitespace-pre-wrap text-sm leading-relaxed">
                        {msg.content}
                      </div>
                    </div>
                  </div>
                )}
              </div>
            ))}
            <div ref={messagesEndRef} />
          </div>

          {/* Input */}
          <div className="bg-white border-t border-gray-200/50 px-6 py-4 flex-shrink-0">
            <div className="relative max-w-4xl mx-auto">
              <input
                ref={inputRef}
                type="text"
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyPress={handleKeyPress}
                placeholder="Type a command or ask a question..."
                disabled={isStreaming}
                className="w-full px-4 py-3 pr-12 bg-white border-2 border-gray-200 rounded-2xl focus:outline-none focus:border-gray-400 focus:ring-2 focus:ring-gray-100 text-sm transition-all disabled:bg-gray-100 disabled:cursor-not-allowed shadow-sm"
              />
              <button
                onClick={handleSend}
                disabled={isStreaming || !input.trim()}
                className="absolute right-2 top-1/2 transform -translate-y-1/2 p-2 text-gray-600 hover:text-gray-800 disabled:opacity-50 disabled:cursor-not-allowed transition-all"
              >
                {isStreaming ? (
                  <div className="w-4 h-4 border-2 border-gray-600 border-t-transparent rounded-full animate-spin" />
                ) : (
                  <SendHorizontal className="w-4 h-4" />
                )}
              </button>
            </div>
          </div>
        </div>

        {/* ─── Collapsible Dashboard Sidebar ──────────────────────────── */}
        <div
          className={`transition-all duration-300 ease-in-out border-l border-gray-200/50 bg-gray-50 flex flex-col overflow-hidden ${
            sidebarOpen ? "w-96" : "w-0"
          }`}
        >
          {sidebarOpen && (
            <div className="flex flex-col gap-3 p-3 h-full overflow-y-auto custom-scrollbar">
              {/* Agent Health — compact */}
              <div className="flex-shrink-0 h-64">
                <AgentHealth />
              </div>

              {/* Approval Queue */}
              <div className="flex-shrink-0 h-72">
                <ApprovalQueue />
              </div>

              {/* Activity Feed — fills remaining space */}
              <div className="flex-1 min-h-64">
                <AgentActivityFeed />
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Toast notifications */}
      <AgentToast />

      {/* Report Viewer Modal */}
      {reportContent && (
        <ReportViewer
          content={reportContent}
          onClose={() => setReportContent(null)}
        />
      )}
    </>
  );
}
