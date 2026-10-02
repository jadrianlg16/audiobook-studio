import type { ReactNode } from "react";
import { Link, NavLink } from "react-router-dom";

import { useApi } from "../api/client";
import { useHealth } from "./health";

/** Shelf, Jobs, Settings, Lab — the same four on every screen, in this order. */
export const NAV_LINKS: { to: string; label: string; end?: boolean }[] = [
  { to: "/", label: "Shelf", end: true },
  { to: "/jobs", label: "Jobs" },
  { to: "/settings", label: "Settings" },
  { to: "/lab", label: "Lab" },
];

export function NavLinks() {
  return (
    <nav className="navlinks">
      {NAV_LINKS.map((l) => (
        <NavLink
          key={l.to}
          to={l.to}
          end={l.end}
          className={({ isActive }) => (isActive ? "on" : "")}
        >
          {l.label}
        </NavLink>
      ))}
    </nav>
  );
}

/**
 * GET /health, as a pill.
 *
 * It answers the question every other screen quietly assumed: is the studio
 * even there, and is what I am hearing real. `offline` means the fake synth
 * produced the audio; `fake_director` means the notes were canned, not written
 * by a model. Both used to be visible only on the settings screen, which is the
 * one screen where nobody is judging a take.
 */
export function HealthPill() {
  const api = useApi();
  const { health, error, loading } = useHealth(api);

  if (loading && !health && !error) {
    return (
      <span className="pill" title="Asking the studio service how it is.">
        <span className="pill-dot warn" aria-hidden />
        checking
      </span>
    );
  }

  if (!health) {
    return (
      <span
        className="pill bad"
        title={`GET /health failed: ${error ?? "no answer"}. The studio service is not answering; nothing on this screen can be saved.`}
      >
        <span className="pill-dot bad" aria-hidden />
        studio unreachable
      </span>
    );
  }

  const gatewayDown = health.gateway !== "ok";
  return (
    <span className="pillgroup">
      <span
        className={`pill${gatewayDown ? " warn" : ""}`}
        title={
          gatewayDown
            ? "The studio is up, but it cannot reach the gateway: engine calls will fail until it can."
            : "The studio is up and the gateway is answering."
        }
      >
        <span className={`pill-dot ${gatewayDown ? "warn" : "ok"}`} aria-hidden />
        {gatewayDown ? "gateway unreachable" : "studio ok"}
      </span>
      {health.offline && (
        <span
          className="pill flag"
          title="STUDIO_OFFLINE=1 — synthesis is faked. Anything rendered in this mode is a test tone, not speech."
        >
          offline · fake synth
        </span>
      )}
      {health.fake_director && (
        <span
          className="pill flag"
          title="STUDIO_FAKE_DIRECTOR=1 — direction is canned, not written by a model."
        >
          fake director
        </span>
      )}
    </span>
  );
}

/**
 * The bar at the top of every studio screen: where you are on the left,
 * the screen's own actions in the middle-right, health and navigation last.
 */
export function TopBar({ crumbs, children }: { crumbs: ReactNode; children?: ReactNode }) {
  return (
    <header className="topbar">
      <div className="title">
        <Link to="/" className="eyebrow" style={{ letterSpacing: "0.14em" }}>
          STUDIO
        </Link>
        <span className="faint">/</span>
        {crumbs}
      </div>
      {/* The screen's actions, health and navigation are one right-hand group:
          two competing `margin-left: auto` boxes used to split the slack and
          strand the actions in the middle of the bar, and a wrapped second row
          restarted at the left margin. */}
      <div className="topbar-right">
        {children}
        <HealthPill />
        <NavLinks />
      </div>
    </header>
  );
}
