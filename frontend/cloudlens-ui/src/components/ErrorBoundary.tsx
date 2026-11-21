import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
}

interface State {
  hasError: boolean;
}

/** Catches render-time errors so one failing component doesn't blank the page. */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false };

  static getDerivedStateFromError(): State {
    return { hasError: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Log locally only; never ship error details to third parties.
    console.error("CloudLens UI error", error, info.componentStack);
  }

  render(): ReactNode {
    if (this.state.hasError) {
      return (
        <div
          role="alert"
          className="mx-auto mt-16 max-w-xl rounded-md border border-red-400/30 bg-red-500/10 px-4 py-3 text-sm text-red-200"
        >
          Something went wrong rendering this page. Reload to try again.
        </div>
      );
    }
    return this.props.children;
  }
}
