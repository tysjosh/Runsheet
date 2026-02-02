'use client';

import React, { Component, ErrorInfo, ReactNode } from 'react';
import { AlertTriangle, RefreshCw } from 'lucide-react';

interface ErrorBoundaryProps {
  children: ReactNode;
  /** Name of the component being wrapped, used for display and logging */
  componentName?: string;
  /** Custom fallback UI to render when an error occurs */
  fallback?: ReactNode;
  /** Callback function called when an error is caught */
  onError?: (error: Error, errorInfo: ErrorInfo) => void;
  /** Custom retry handler - if not provided, component will reset state */
  onRetry?: () => void;
}

interface ErrorBoundaryState {
  hasError: boolean;
  error: Error | null;
  errorInfo: ErrorInfo | null;
}

/**
 * ErrorBoundary component that catches JavaScript errors in child component tree,
 * logs those errors, and displays a fallback UI with retry option.
 * 
 * Validates: Requirements 9.1, 9.2
 * - Requirement 9.1: Implements React error boundaries around major components
 * - Requirement 9.2: Displays user-friendly error message with retry option
 */
export default class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  constructor(props: ErrorBoundaryProps) {
    super(props);
    this.state = {
      hasError: false,
      error: null,
      errorInfo: null,
    };
  }

  static getDerivedStateFromError(error: Error): Partial<ErrorBoundaryState> {
    // Update state so the next render will show the fallback UI
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo): void {
    // Log the error to console for debugging
    console.error(`ErrorBoundary caught an error in ${this.props.componentName || 'component'}:`, error);
    console.error('Component stack:', errorInfo.componentStack);

    // Update state with error info
    this.setState({ errorInfo });

    // Call optional error callback
    if (this.props.onError) {
      this.props.onError(error, errorInfo);
    }
  }

  handleRetry = (): void => {
    // If custom retry handler is provided, call it
    if (this.props.onRetry) {
      this.props.onRetry();
    }

    // Reset error state to re-render children
    this.setState({
      hasError: false,
      error: null,
      errorInfo: null,
    });
  };

  render(): ReactNode {
    const { hasError, error } = this.state;
    const { children, componentName, fallback } = this.props;

    if (hasError) {
      // If custom fallback is provided, use it
      if (fallback) {
        return fallback;
      }

      // Default fallback UI with user-friendly error message and retry option
      return (
        <div className="h-full flex items-center justify-center bg-gray-50 p-6">
          <div className="max-w-md w-full bg-white rounded-xl shadow-sm border border-gray-200 p-8 text-center">
            {/* Error Icon */}
            <div className="w-16 h-16 bg-red-50 rounded-full flex items-center justify-center mx-auto mb-6">
              <AlertTriangle className="w-8 h-8 text-red-500" />
            </div>

            {/* Error Title */}
            <h2 className="text-xl font-semibold text-gray-900 mb-2">
              Something went wrong
            </h2>

            {/* Component Name */}
            {componentName && (
              <p className="text-sm text-gray-500 mb-4">
                Error in {componentName}
              </p>
            )}

            {/* User-friendly Error Message */}
            <p className="text-gray-600 mb-6">
              We encountered an unexpected error while loading this section. 
              Please try again or contact support if the problem persists.
            </p>

            {/* Error Details (collapsed by default in production) */}
            {process.env.NODE_ENV === 'development' && error && (
              <details className="mb-6 text-left">
                <summary className="text-sm text-gray-500 cursor-pointer hover:text-gray-700">
                  Technical details
                </summary>
                <pre className="mt-2 p-3 bg-gray-100 rounded-lg text-xs text-red-600 overflow-auto max-h-32">
                  {error.message}
                  {error.stack && `\n\n${error.stack}`}
                </pre>
              </details>
            )}

            {/* Retry Button */}
            <button
              onClick={this.handleRetry}
              className="inline-flex items-center gap-2 px-6 py-3 bg-[#232323] hover:bg-gray-800 text-white rounded-xl font-medium transition-colors"
            >
              <RefreshCw className="w-4 h-4" />
              Try Again
            </button>

            {/* Additional Help */}
            <p className="mt-4 text-sm text-gray-400">
              If this problem continues, please refresh the page or contact support.
            </p>
          </div>
        </div>
      );
    }

    return children;
  }
}

/**
 * Higher-order component to wrap a component with ErrorBoundary
 * Usage: const SafeComponent = withErrorBoundary(MyComponent, 'MyComponent');
 */
export function withErrorBoundary<P extends object>(
  WrappedComponent: React.ComponentType<P>,
  componentName: string,
  errorBoundaryProps?: Omit<ErrorBoundaryProps, 'children' | 'componentName'>
): React.FC<P> {
  const WithErrorBoundary: React.FC<P> = (props) => (
    <ErrorBoundary componentName={componentName} {...errorBoundaryProps}>
      <WrappedComponent {...props} />
    </ErrorBoundary>
  );

  WithErrorBoundary.displayName = `WithErrorBoundary(${componentName})`;
  return WithErrorBoundary;
}
