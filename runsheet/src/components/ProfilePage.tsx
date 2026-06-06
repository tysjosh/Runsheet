"use client";

/**
 * User profile / account page.
 *
 * Shows the signed-in user's identity (email, tenant, roles, PII access) read
 * from the backend `GET /api/auth/account/me` — which derives everything from
 * the verified SuperTokens session, so it always reflects the real identity.
 * The change-password form is embedded here so "click avatar → profile →
 * change password" is a single, conventional flow.
 */

import { ShieldCheck, User } from "lucide-react";
import { useEffect, useState } from "react";
import { apiService } from "../services/api";
import ChangePassword from "./ChangePassword";
import LoadingSpinner from "./LoadingSpinner";
import { PageHeader } from "./ui";

interface AccountProfile {
  user_id: string;
  email: string;
  tenant_id: string;
  roles: string[];
  has_pii_access: boolean;
}

export default function ProfilePage() {
  const [profile, setProfile] = useState<AccountProfile | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await apiService.getAccountProfile();
        if (cancelled) return;
        if (data) {
          setProfile(data);
        } else {
          setError("Could not load your profile.");
        }
      } catch {
        if (!cancelled) setError("Could not load your profile.");
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="flex flex-col h-full">
      <PageHeader title="My Account" />
      <div className="flex-1 overflow-auto p-6">
        {isLoading ? (
          <LoadingSpinner message="Loading your profile..." />
        ) : (
          <div className="max-w-3xl space-y-6">
            {/* Identity card */}
            <div className="bg-white border border-gray-200 rounded-xl p-6">
              <div className="flex items-center gap-4">
                <div className="w-14 h-14 rounded-full flex items-center justify-center bg-primary flex-shrink-0">
                  <User className="w-7 h-7 text-white" />
                </div>
                <div className="min-w-0">
                  <p className="text-lg font-semibold text-gray-900 truncate">
                    {profile?.email || "Unknown user"}
                  </p>
                  <p className="text-sm text-gray-500">
                    Tenant: {profile?.tenant_id || "—"}
                  </p>
                </div>
              </div>

              {error && (
                <p className="mt-4 text-sm text-error" role="alert">
                  {error}
                </p>
              )}

              <dl className="mt-6 grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div>
                  <dt className="text-xs font-medium uppercase tracking-wider text-gray-500 mb-1">
                    Roles
                  </dt>
                  <dd className="flex flex-wrap gap-2">
                    {profile?.roles?.length ? (
                      profile.roles.map((role) => (
                        <span
                          key={role}
                          className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-blue-50 text-blue-700"
                        >
                          {role}
                        </span>
                      ))
                    ) : (
                      <span className="text-sm text-gray-500">No roles</span>
                    )}
                  </dd>
                </div>
                <div>
                  <dt className="text-xs font-medium uppercase tracking-wider text-gray-500 mb-1">
                    PII access
                  </dt>
                  <dd>
                    {profile?.has_pii_access ? (
                      <span className="inline-flex items-center gap-1 text-sm text-green-700">
                        <ShieldCheck className="w-4 h-4" />
                        Granted
                      </span>
                    ) : (
                      <span className="text-sm text-gray-500">Not granted</span>
                    )}
                  </dd>
                </div>
              </dl>
            </div>

            {/* Change password */}
            <div className="bg-white border border-gray-200 rounded-xl">
              <ChangePassword />
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
