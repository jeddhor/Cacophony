/**
 * The application shell (design document sections 45, 46).
 *
 * Section 46's navigation, verbatim: Projects, Studio, Generate, Runs,
 * Providers, Assets, Plugins, Settings. Plugins belongs to a later phase and is
 * shown disabled rather than hidden - the shape of the product
 * is part of what the interface communicates, and a destination that appears
 * later without warning is more disorienting than one that was always visible.
 */

import type { ReactNode } from "react";
import { NavLink, useLocation } from "react-router-dom";

import { useSystem } from "../api/hooks";
import { useStudio } from "../state/store";

interface Destination {
  to: string;
  label: string;
  glyph: string;
  /** Needs a project selected before it means anything. */
  needsProject?: boolean;
  /** Arrives in a later phase. */
  later?: string;
}

const DESTINATIONS: Destination[] = [
  { to: "/projects", label: "Projects", glyph: "◈" },
  { to: "/studio", label: "Studio", glyph: "◆", needsProject: true },
  { to: "/generate", label: "Generate", glyph: "▶", needsProject: true },
  { to: "/stream", label: "Stream", glyph: "≋", needsProject: true },
  { to: "/runs", label: "Runs", glyph: "◉" },
  { to: "/providers", label: "Providers", glyph: "⌁" },
  { to: "/assets", label: "Assets", glyph: "▣" },
  { to: "/plugins", label: "Plugins", glyph: "⬡", later: "plugin phase" },
  { to: "/settings", label: "Settings", glyph: "⚙" },
];

/**
 * The section 45 motif: many voices at different frequencies converging into
 * one organised stream. Drawn rather than imported so it costs nothing.
 */
function Waveform(): ReactNode {
  const bars = [4, 11, 6, 16, 9, 20, 7, 13, 5, 17, 8, 12, 6, 14, 4];
  const hues = ["var(--violet)", "var(--cyan)", "var(--magenta)"];
  return (
    <svg className="waveform" viewBox="0 0 120 20" aria-hidden="true" focusable="false">
      {bars.map((height, index) => (
        <rect
          key={index}
          x={index * 8 + 1}
          y={(20 - height) / 2}
          width="3"
          height={height}
          rx="1.5"
          fill={hues[index % hues.length]}
          opacity={0.35 + (height / 20) * 0.5}
        />
      ))}
    </svg>
  );
}

export function Layout({ children }: { children: ReactNode }): ReactNode {
  const projectId = useStudio((state) => state.projectId);
  const system = useSystem();
  const location = useLocation();

  return (
    <div className="shell">
      <nav className="sidebar" aria-label="Primary">
        <div className="brand">
          <div className="brand-mark">CACOPHONY</div>
          <div className="brand-tag">a synthetic reality compiler</div>
          <Waveform />
        </div>

        <div className="nav">
          {DESTINATIONS.map((destination) => {
            const blocked = destination.later
              ? `Arrives in the ${destination.later}`
              : destination.needsProject && projectId === null
                ? "Select a project first"
                : null;

            if (blocked) {
              return (
                <span
                  key={destination.to}
                  className="nav-link disabled"
                  title={blocked}
                  aria-disabled="true"
                >
                  <span className="glyph" aria-hidden="true">
                    {destination.glyph}
                  </span>
                  {destination.label}
                </span>
              );
            }

            return (
              <NavLink
                key={destination.to}
                to={destination.to}
                className={({ isActive }) =>
                  `nav-link ${isActive || location.pathname.startsWith(`${destination.to}/`) ? "active" : ""}`
                }
              >
                <span className="glyph" aria-hidden="true">
                  {destination.glyph}
                </span>
                {destination.label}
              </NavLink>
            );
          })}
        </div>

        <div className="sidebar-foot">
          {system.data ? (
            <>
              <div>v{system.data.version}</div>
              <div title={system.data.store.path}>
                {system.data.runs} run{system.data.runs === 1 ? "" : "s"} recorded
              </div>
              {system.data.active_runs.length > 0 && (
                <div style={{ color: "var(--cyan)" }}>
                  {system.data.active_runs.length} running
                </div>
              )}
            </>
          ) : system.isError ? (
            <span style={{ color: "var(--red)" }}>API unreachable</span>
          ) : (
            <span>connecting…</span>
          )}
        </div>
      </nav>

      <main className="main">{children}</main>
    </div>
  );
}

export function PageHead({
  title,
  subtitle,
  actions,
}: {
  title: string;
  subtitle?: ReactNode;
  actions?: ReactNode;
}): ReactNode {
  return (
    <header className="page-head">
      <div>
        <h1>{title}</h1>
        {subtitle && <div className="subtitle">{subtitle}</div>}
      </div>
      {actions && <div className="row">{actions}</div>}
    </header>
  );
}
