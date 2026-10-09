import type { Metadata } from "next";
import Link from "next/link";

/**
 * Privacy notice for the public marketing site.
 *
 * Scope is deliberately narrow: it covers the data the PUBLIC site collects,
 * which today is exactly the five fields on `/request-pilot` plus the HubSpot
 * cookie their embed may set. It does not attempt to describe the authenticated
 * application's data handling — operational data belongs to the tenant, is
 * governed by their agreement, and describing it here would be guesswork.
 *
 * Every factual claim below is traceable to code:
 *   - the field list matches `PilotForm` in `app/request-pilot/page.tsx`
 *   - the HubSpot forwarding matches `app/api/pilot-request/lead.ts`
 *   - "we do not sell your information" is true because there is exactly one
 *     outbound recipient in the code path, and it is HubSpot
 *
 * Keep it that way. If the form gains a field or the route gains a recipient,
 * this page is part of that change, not a follow-up.
 *
 * NOT LEGAL ADVICE. This is a factual description written by engineers. Have
 * counsel review it before relying on it for CCPA/CPRA or GDPR compliance,
 * particularly the rights section and the retention period, which is currently
 * stated as indefinite because nothing deletes HubSpot contacts.
 */

const CONTACT_EMAIL = "hello@runsheet.app";

/** Rendered as the "last updated" date. Bump when the substance changes. */
const LAST_UPDATED = "August 2026";

export const metadata: Metadata = {
  title: "Privacy",
  description:
    "What Runsheet collects from this website, why, who it is shared with, and how to have it deleted.",
};

function Section({
  heading,
  children,
}: {
  heading: string;
  children: React.ReactNode;
}) {
  return (
    <section className="mt-12">
      <h2 className="text-xl font-black uppercase tracking-tight text-[#f5f4ef]">
        {heading}
      </h2>
      <div className="mt-4 space-y-4 text-sm leading-relaxed text-[#f5f4ef]/70">
        {children}
      </div>
    </section>
  );
}

export default function PrivacyPage() {
  return (
    <div className="min-h-screen bg-[#0a0a0b] text-[#f5f4ef] antialiased">
      <header className="border-b border-[#f5f4ef]/10">
        <div className="mx-auto flex max-w-3xl items-center justify-between px-6 py-4 lg:px-10">
          <Link href="/" className="flex items-baseline gap-px">
            <span className="text-lg font-black uppercase tracking-tight">
              RUN<span className="text-[#16b88c]">/</span>SHEET
            </span>
          </Link>
          <Link
            href="/request-pilot"
            className="font-mono text-[11px] uppercase tracking-[0.2em] text-[#f5f4ef]/60 transition-colors hover:text-[#f5f4ef]"
          >
            Request a Pilot
          </Link>
        </div>
      </header>

      <main className="mx-auto max-w-3xl px-6 py-16 lg:px-10 lg:py-24">
        <div className="flex items-center gap-3 font-mono text-[11px] uppercase tracking-[0.3em] text-[#16b88c]">
          <span className="h-px w-8 bg-[#16b88c]" />
          Privacy
        </div>

        <h1 className="mt-6 text-[clamp(2.5rem,7vw,4rem)] font-black uppercase leading-[0.9] tracking-[-0.03em]">
          Privacy notice
        </h1>

        <p className="mt-6 font-mono text-xs uppercase tracking-[0.2em] text-[#f5f4ef]/45">
          Last updated {LAST_UPDATED}
        </p>

        <p className="mt-8 text-base leading-relaxed text-[#f5f4ef]/80">
          This notice covers the Runsheet public website. It explains what we
          collect when you ask for a pilot, why we collect it, who it goes to,
          and how to have it removed. It does not cover data inside the Runsheet
          application, which is handled under the agreement with the customer
          whose account holds it.
        </p>

        <Section heading="What we collect">
          <p>
            Only what you type into the Request a Pilot form. That is your full
            name, work email address, company name, the fleet-size range you
            select, and — optionally — the free-text description of what you
            want to solve.
          </p>
          <p>
            The form has no analytics, no advertising trackers and no
            fingerprinting. We do not run a tracking pixel on this site.
          </p>
        </Section>

        <Section heading="Why we collect it">
          <p>
            To reply to you about a pilot, and to keep a record of the
            conversation. We do not use it for anything else, and we do not add
            you to a marketing list from this form.
          </p>
        </Section>

        <Section heading="Who it is shared with">
          <p>
            Your submission is stored in{" "}
            <a
              href="https://legal.hubspot.com/privacy-policy"
              className="text-[#16b88c] hover:underline"
              target="_blank"
              rel="noopener noreferrer"
            >
              HubSpot
            </a>
            , the CRM we use to manage sales conversations. HubSpot processes it
            on our behalf and on servers in the United States.
          </p>
          <p>
            That is the only third party your form submission is sent to. We do
            not sell your information, and we do not share it for advertising.
          </p>
        </Section>

        <Section heading="How long we keep it">
          <p>
            We keep pilot requests in HubSpot until you ask us to delete them,
            or until we no longer have a reason to hold them. We do not
            currently run an automatic deletion schedule, so if you want your
            details removed the reliable route is to ask.
          </p>
        </Section>

        <Section heading="Your choices">
          <p>
            Email{" "}
            <a
              href={`mailto:${CONTACT_EMAIL}`}
              className="text-[#16b88c] hover:underline"
            >
              {CONTACT_EMAIL}
            </a>{" "}
            and we will tell you what we hold about you, correct it, or delete
            it. You do not need to give a reason, and we will not charge you.
          </p>
          <p>
            If you are in California, the CCPA gives you rights to know, delete,
            correct, and opt out of sale or sharing. We do not sell or share
            personal information as those terms are defined, so there is nothing
            to opt out of — but the know, delete and correct requests all work
            through the same address above.
          </p>
        </Section>

        <Section heading="Security">
          <p>
            The site is served over HTTPS, and your submission is sent to
            HubSpot over an encrypted connection from our server rather than
            from your browser. Beyond that, your submission lives in HubSpot and
            is protected by their controls and by access limits on our HubSpot
            account.
          </p>
        </Section>

        <Section heading="Changes">
          <p>
            If we start collecting something new, or send your details somewhere
            new, we will update this page and change the date at the top. There
            is no version history yet; if that matters to you, ask and we will
            tell you what changed.
          </p>
        </Section>

        <Section heading="Contact">
          <p>
            Questions about any of this go to{" "}
            <a
              href={`mailto:${CONTACT_EMAIL}`}
              className="text-[#16b88c] hover:underline"
            >
              {CONTACT_EMAIL}
            </a>
            .
          </p>
        </Section>

        <div className="mt-16 border-t border-[#f5f4ef]/10 pt-8">
          <Link
            href="/"
            className="inline-flex items-center gap-2 rounded-full border border-[#f5f4ef]/20 px-6 py-3 text-sm font-bold uppercase tracking-[0.12em] transition-all hover:border-[#f5f4ef]/50"
          >
            Back to home
          </Link>
        </div>
      </main>
    </div>
  );
}
