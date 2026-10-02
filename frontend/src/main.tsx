import React, { lazy, Suspense, useEffect, useMemo, useState } from "react";
import type { ComponentType, ReactNode } from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import type { StudioApi } from "./studio/api/client";
import { ApiContext, resolveMockMode } from "./studio/api/client";
import { createRealApi } from "./studio/api/real";
import { Spinner } from "./studio/ui/bits";
import { RouteBoundary } from "./studio/ui/RouteErrorBoundary";
import { NavLinks } from "./studio/ui/TopBar";
import "./index.css";

/**
 * Every route is its own chunk, fetched the first time it is visited: the shelf
 * does not download the desk, and nobody who never opens /lab downloads the
 * bake-off lab. Vite names each chunk after its module (Desk-*.js, App-*.js…).
 */
function lazyScreen<M extends Record<K, ComponentType>, K extends string>(
  load: () => Promise<M>,
  name: K,
) {
  return lazy(() => load().then((m) => ({ default: m[name] })));
}

const LabApp = lazy(() => import("./lab/App"));
const Shelf = lazyScreen(() => import("./studio/routes/Shelf"), "Shelf");
const NewBook = lazyScreen(() => import("./studio/routes/NewBook"), "NewBook");
const Desk = lazyScreen(() => import("./studio/routes/Desk"), "Desk");
const CastSheet = lazyScreen(() => import("./studio/routes/CastSheet"), "CastSheet");
const LexiconScreen = lazyScreen(() => import("./studio/routes/LexiconScreen"), "LexiconScreen");
const Review = lazyScreen(() => import("./studio/routes/Review"), "Review");
const JobsScreen = lazyScreen(() => import("./studio/routes/JobsScreen"), "JobsScreen");
const JobScreen = lazyScreen(() => import("./studio/routes/JobScreen"), "JobScreen");
const SettingsScreen = lazyScreen(() => import("./studio/routes/SettingsScreen"), "SettingsScreen");

/**
 * Every route renders inside its own error boundary: a screen that throws while
 * rendering — or whose chunk fails to load — shows what went wrong, and
 * navigating anywhere resets it. While a chunk loads, the route shows the same
 * "loading…" line every screen shows while its data loads.
 */
const guarded = (element: ReactNode, loading = "opening the screen") => (
  <RouteBoundary>
    <Suspense
      fallback={
        <div className="page">
          <Spinner label={loading} />
        </div>
      }
    >
      {element}
    </Suspense>
  </RouteBoundary>
);

function Root() {
  // Decided once per page load: VITE_MOCK=1, ?mock=1 (or, in a dev build only,
  // a remembered localStorage.studioMock).
  const mock = useMemo(() => resolveMockMode(), []);
  const [api, setApi] = useState<StudioApi | null>(null);

  // The mock is ~50 kB of fixtures. Importing it dynamically keeps it out of the
  // main chunk, so a production build only ever fetches it if somebody asks for
  // mock mode.
  useEffect(() => {
    let alive = true;
    if (!mock) {
      setApi(createRealApi());
      return;
    }
    void import("./studio/api/mock").then((m) => {
      if (alive) setApi(m.createMockApi());
    });
    return () => {
      alive = false;
    };
  }, [mock]);

  if (!api) {
    return (
      <div className="studio">
        <p className="mono faint" style={{ padding: 24 }}>
          starting the studio…
        </p>
      </div>
    );
  }

  return (
    <ApiContext.Provider value={api}>
      <BrowserRouter>
        <Routes>
          <Route
            path="/lab"
            element={
              <div className="lab-root">
                {/* The lab is the older bake-off tool, not a second product. It
                    said so nowhere, and linked only one way. */}
                {/* The lab keeps its own dark surface, but not its own
                    navigation: the four links are the same on every screen. */}
                <div className="labnote">
                  <span className="labnote-title">Engine bake-off lab</span>
                  <span className="labnote-body">
                    Compare engines on one passage. Nothing here touches a book.
                  </span>
                  <span className="labnote-nav">
                    <NavLinks />
                  </span>
                </div>
                {guarded(<LabApp />, "opening the lab")}
              </div>
            }
          />
          <Route
            path="*"
            element={
              <div className="studio">
                <Routes>
                  <Route path="/" element={guarded(<Shelf />)} />
                  <Route path="/books/new" element={guarded(<NewBook />)} />
                  <Route path="/books/:slug" element={guarded(<Desk />)} />
                  <Route path="/books/:slug/cast" element={guarded(<CastSheet />)} />
                  <Route path="/books/:slug/lexicon" element={guarded(<LexiconScreen />)} />
                  <Route path="/books/:slug/review/:passId" element={guarded(<Review />)} />
                  <Route path="/jobs" element={guarded(<JobsScreen />)} />
                  <Route path="/jobs/:id" element={guarded(<JobScreen />)} />
                  <Route path="/settings" element={guarded(<SettingsScreen />)} />
                  <Route path="*" element={<Navigate to="/" replace />} />
                </Routes>
                {mock && <div className="mockflag">MOCK DATA</div>}
              </div>
            }
          />
        </Routes>
      </BrowserRouter>
    </ApiContext.Provider>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <Root />
  </React.StrictMode>,
);
