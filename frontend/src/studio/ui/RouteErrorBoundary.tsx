import { Component, useId } from "react";
import type { ReactNode } from "react";
import { Link, useLocation } from "react-router-dom";

/**
 * Catches an error thrown while a route renders, so one broken screen shows
 * what went wrong instead of unmounting the whole app to a blank page.
 *
 * React only catches render errors in a class component. It does not catch
 * errors in event handlers or in async code (those already land in each
 * screen's ErrorBox).
 */
interface BoundaryProps {
  children: ReactNode;
  /**
   * When this changes while the fallback is showing, the boundary clears
   * itself and renders its children again. RouteBoundary passes the
   * location's key, so navigating anywhere — even to the same URL — retries.
   */
  resetKey?: unknown;
}

interface BoundaryState {
  error: unknown;
  failed: boolean;
}

export class RouteErrorBoundary extends Component<BoundaryProps, BoundaryState> {
  state: BoundaryState = { error: null, failed: false };

  static getDerivedStateFromError(error: unknown): BoundaryState {
    // `failed` is separate from `error` because a component may throw null.
    return { error, failed: true };
  }

  componentDidUpdate(prev: BoundaryProps) {
    if (this.state.failed && prev.resetKey !== this.props.resetKey) this.reset();
  }

  reset = () => {
    this.setState({ error: null, failed: false });
  };

  render() {
    if (this.state.failed) return <CrashPanel error={this.state.error} onRetry={this.reset} />;
    return this.props.children;
  }
}

/** Wraps a route's element: a boundary that resets when the location changes. */
export function RouteBoundary({ children }: { children: ReactNode }) {
  const location = useLocation();
  return <RouteErrorBoundary resetKey={location.key}>{children}</RouteErrorBoundary>;
}

/** "TypeError: x is undefined", or the bare message for a plain Error. */
export function describeError(error: unknown): string {
  if (error instanceof Error) {
    const name = error.name && error.name !== "Error" ? error.name : "";
    const message = error.message || "(no message)";
    return name ? `${name}: ${message}` : message;
  }
  return typeof error === "string" ? error : String(error);
}

/**
 * No TopBar here on purpose: the top bar is part of every screen, so if it is
 * what threw, drawing it again would throw past this boundary.
 */
function CrashPanel({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const titleId = useId();
  return (
    <div className="page crash">
      <section className="col crash-body" role="alert" aria-labelledby={titleId}>
        <p className="eyebrow">Screen error</p>
        <h1 className="h1" id={titleId}>
          This screen stopped working
        </h1>
        <p className="soft">
          It hit an error while drawing and was stopped here, so the rest of the studio keeps
          working. Try it again, or go back to the shelf.
        </p>
        <pre className="notice risk mono crash-error">{describeError(error)}</pre>
        <div className="row">
          <button type="button" className="btn signal" onClick={onRetry}>
            Try again
          </button>
          <Link className="btn" to="/">
            Back to the shelf
          </Link>
        </div>
      </section>
    </div>
  );
}
