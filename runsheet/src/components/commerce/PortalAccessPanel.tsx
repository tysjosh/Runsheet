"use client";

/**
 * Portal access panel (PD24, design §10.3) on the customer detail page.
 *
 * Admin-only: `CustomerDetailPage` renders it only for `admin`, and the API
 * re-checks. It hides itself when the list answers 404 (portal flag off).
 * Invite and Resend show the password-set link in a read-only field with a
 * Copy button; Resend and Revoke ask for confirmation in the shared Modal.
 */

import { useCallback, useEffect, useId, useState } from "react";
import { Button, Modal, ModalFooter } from "@/components/ui";
import { ApiError } from "../../services/api";
import {
  invitePortalUser,
  listPortalUsers,
  type PortalUserGrant,
  type PortalUserLink,
  resendPortalUserLink,
  revokePortalUser,
} from "../../services/portalApi";
import PortalOrderingSetting from "./PortalOrderingSetting";

type Load =
  | { kind: "loading" }
  | { kind: "hidden" }
  | { kind: "error"; message: string }
  | { kind: "ready"; users: PortalUserGrant[] };

type Pending = { action: "resend" | "revoke"; user: PortalUserGrant } | null;

const STATUS_TEXT: Record<string, string> = {
  invited: "Invited",
  active: "Active",
  revoked: "Revoked",
};

function errorText(error: unknown, fallback: string): string {
  if (error instanceof ApiError && error.message) return error.message;
  return fallback;
}

function formatDate(value: string | null): string {
  if (!value) return "—";
  const d = new Date(value);
  return Number.isNaN(d.getTime())
    ? "—"
    : d.toLocaleDateString("en-US", {
        year: "numeric",
        month: "short",
        day: "numeric",
      });
}

export default function PortalAccessPanel({
  customerId,
}: {
  customerId: string;
}) {
  const uid = useId();
  const [load, setLoad] = useState<Load>({ kind: "loading" });
  const [email, setEmail] = useState("");
  const [inviting, setInviting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [link, setLink] = useState<{ email: string; url: string } | null>(null);
  const [copied, setCopied] = useState(false);
  const [pending, setPending] = useState<Pending>(null);
  const [working, setWorking] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const response = await listPortalUsers(customerId);
      setLoad({ kind: "ready", users: response.data });
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) {
        setLoad({ kind: "hidden" });
        return;
      }
      setLoad({
        kind: "error",
        message: errorText(error, "Portal users couldn't be loaded."),
      });
    }
  }, [customerId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const showLink = (forEmail: string, result: PortalUserLink) => {
    setCopied(false);
    if (result.password_set_link) {
      setLink({ email: forEmail, url: result.password_set_link });
    } else {
      setLink(null);
    }
    const parts: string[] = [];
    if (result.link_error) {
      parts.push("The link couldn't be created. Use Resend to try again.");
    }
    if (result.email_sent) parts.push("The link was also emailed.");
    setNotice(parts.length > 0 ? parts.join(" ") : null);
  };

  const handleInvite = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (inviting) return;
    const value = email.trim();
    if (!value) {
      setFormError("Enter an email address.");
      return;
    }
    setFormError(null);
    setInviting(true);
    try {
      const result = await invitePortalUser(customerId, value);
      showLink(result.email, result);
      if (result.already_invited) {
        setNotice(
          `${result.email} is already invited.${result.email_sent ? " The link was emailed again." : ""}`,
        );
      }
      setEmail("");
      await refresh();
    } catch (error) {
      setFormError(errorText(error, "The invite didn't go through."));
    } finally {
      setInviting(false);
    }
  };

  const handleConfirm = async () => {
    if (!pending || working) return;
    setWorking(true);
    try {
      if (pending.action === "resend") {
        const result = await resendPortalUserLink(
          customerId,
          pending.user.grant_id,
        );
        showLink(pending.user.email, result);
      } else {
        await revokePortalUser(customerId, pending.user.grant_id);
        setLink(null);
        setNotice(`Portal access for ${pending.user.email} was revoked.`);
      }
      setPending(null);
      await refresh();
    } catch (error) {
      setPending(null);
      setNotice(
        errorText(
          error,
          pending.action === "resend"
            ? "The link couldn't be resent."
            : "Access couldn't be revoked.",
        ),
      );
    } finally {
      setWorking(false);
    }
  };

  const handleCopy = async () => {
    if (!link) return;
    try {
      await navigator.clipboard.writeText(link.url);
      setCopied(true);
    } catch {
      setCopied(false);
      setNotice("Copy didn't work. Select the link and copy it manually.");
    }
  };

  if (load.kind === "hidden") return null;

  const headingId = `${uid}-heading`;
  const emailId = `${uid}-email`;
  const linkId = `${uid}-link`;

  return (
    <section aria-labelledby={headingId} className="mb-8">
      <h2 id={headingId} className="text-lg font-semibold mb-3">
        Portal access
      </h2>
      <div className="border rounded p-6 space-y-4">
        <PortalOrderingSetting />
        <form onSubmit={handleInvite} noValidate className="space-y-2">
          <label htmlFor={emailId} className="block text-sm font-medium">
            Invite by email
          </label>
          <div className="flex flex-wrap gap-2">
            <input
              id={emailId}
              type="email"
              autoComplete="off"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              aria-invalid={formError ? true : undefined}
              aria-describedby={formError ? `${emailId}-error` : undefined}
              className="min-w-0 flex-1 rounded-lg border border-gray-300 px-3 py-2 text-sm"
            />
            <Button type="submit" loading={inviting}>
              Invite
            </Button>
          </div>
          {formError && (
            <p
              id={`${emailId}-error`}
              role="alert"
              className="text-sm text-error-dark"
            >
              {formError}
            </p>
          )}
        </form>

        {link && (
          <div className="space-y-1">
            <label htmlFor={linkId} className="block text-sm font-medium">
              Password-set link for {link.email}
            </label>
            <div className="flex flex-wrap gap-2">
              <input
                id={linkId}
                type="text"
                readOnly
                value={link.url}
                onFocus={(e) => e.currentTarget.select()}
                className="min-w-0 flex-1 rounded-lg border border-gray-300 bg-gray-50 px-3 py-2 font-mono text-xs"
              />
              <Button type="button" variant="secondary" onClick={handleCopy}>
                {copied ? "Copied" : "Copy"}
              </Button>
            </div>
            <p className="text-xs text-gray-600">
              Send this link to the customer. It works once and expires.
            </p>
          </div>
        )}

        <p role="status" className="text-sm text-gray-700">
          {notice ?? ""}
        </p>

        {load.kind === "loading" && (
          <p className="text-sm text-gray-600">Loading portal users…</p>
        )}
        {load.kind === "error" && (
          <div className="flex flex-wrap items-center gap-2">
            <p role="alert" className="text-sm text-error-dark">
              {load.message}
            </p>
            <Button type="button" variant="ghost" onClick={() => refresh()}>
              Retry
            </Button>
          </div>
        )}
        {load.kind === "ready" &&
          (load.users.length === 0 ? (
            <p className="text-sm text-gray-600">No portal users yet.</p>
          ) : (
            <table className="w-full text-left text-sm">
              <caption className="sr-only">Portal users</caption>
              <thead>
                <tr className="border-b text-gray-600">
                  <th scope="col" className="py-2 pr-3 font-medium">
                    Email
                  </th>
                  <th scope="col" className="py-2 pr-3 font-medium">
                    Status
                  </th>
                  <th scope="col" className="py-2 pr-3 font-medium">
                    Invited
                  </th>
                  <th scope="col" className="py-2 font-medium">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {load.users.map((user) => (
                  <tr key={user.grant_id} className="border-b last:border-0">
                    <td className="py-2 pr-3 break-all">{user.email}</td>
                    <td className="py-2 pr-3">
                      {STATUS_TEXT[user.status] ?? user.status}
                    </td>
                    <td className="py-2 pr-3">{formatDate(user.created_at)}</td>
                    <td className="py-2">
                      {user.status !== "revoked" && (
                        <div className="flex flex-wrap gap-2">
                          <Button
                            type="button"
                            size="sm"
                            variant="secondary"
                            aria-label={`Resend link to ${user.email}`}
                            onClick={() =>
                              setPending({ action: "resend", user })
                            }
                          >
                            Resend
                          </Button>
                          <Button
                            type="button"
                            size="sm"
                            variant="danger"
                            aria-label={`Revoke access for ${user.email}`}
                            onClick={() =>
                              setPending({ action: "revoke", user })
                            }
                          >
                            Revoke
                          </Button>
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ))}
      </div>

      <Modal
        isOpen={pending !== null}
        onClose={() => (working ? undefined : setPending(null))}
        title={
          pending?.action === "revoke"
            ? "Revoke portal access?"
            : "Resend the password-set link?"
        }
        size="sm"
        footer={
          <ModalFooter
            onCancel={() => setPending(null)}
            onConfirm={handleConfirm}
            confirmText={pending?.action === "revoke" ? "Revoke" : "Resend"}
            confirmVariant={pending?.action === "revoke" ? "danger" : "primary"}
            loading={working}
          />
        }
      >
        <p className="text-sm text-gray-700">
          {pending?.action === "revoke"
            ? `${pending.user.email} will be signed out and won't be able to sign in to the portal again.`
            : `A new link will be created for ${pending?.user.email ?? ""}.`}
        </p>
      </Modal>
    </section>
  );
}
