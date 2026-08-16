/**
 * Routing (design document section 46).
 *
 * Routes match the navigation exactly, and the run inspector takes the run id
 * in its URL so a live run can be linked to, bookmarked and reloaded - which
 * is what makes a nine-hour generation something you can walk away from.
 */

import type { ReactNode } from "react";
import { Navigate, Route, Routes } from "react-router-dom";

import { Layout } from "./components/Layout";
import { Empty } from "./components/ui";
import { GeneratePage } from "./pages/GeneratePage";
import { AssetsPage } from "./pages/AssetsPage";
import { ProjectsPage } from "./pages/ProjectsPage";
import { ProvidersPage } from "./pages/ProvidersPage";
import { RunPage } from "./pages/RunPage";
import { RunsPage } from "./pages/RunsPage";
import { SettingsPage } from "./pages/SettingsPage";
import { StudioPage } from "./pages/StudioPage";

function Later({ what, phase }: { what: string; phase: string }): ReactNode {
  return (
    <Empty title={what}>
      <p>
        Arrives in the {phase}. The schema already carries the declarations it
        will read, so nothing written now will need rewriting.
      </p>
    </Empty>
  );
}

export function App(): ReactNode {
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Navigate to="/projects" replace />} />
        <Route path="/projects" element={<ProjectsPage />} />
        <Route path="/studio" element={<StudioPage />} />
        <Route path="/generate" element={<GeneratePage />} />
        <Route path="/runs" element={<RunsPage />} />
        <Route path="/runs/:runId" element={<RunPage />} />
        <Route path="/providers" element={<ProvidersPage />} />
        <Route path="/assets" element={<AssetsPage />} />
        <Route path="/plugins" element={<Later what="Plugins" phase="plugin phase" />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route
          path="*"
          element={
            <Empty title="Not found">
              <p>That page does not exist.</p>
            </Empty>
          }
        />
      </Routes>
    </Layout>
  );
}
