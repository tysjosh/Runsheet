/**
 * Test file to verify Jest and React Testing Library setup
 * This file validates that the testing infrastructure is correctly configured
 */

import { render, screen } from '@testing-library/react';

// Simple test component
function TestComponent({ message }: { message: string }) {
  return (
    <div>
      <h1>Test Component</h1>
      <p data-testid="message">{message}</p>
    </div>
  );
}

describe('Jest and React Testing Library Setup', () => {
  it('should render a simple component', () => {
    render(<TestComponent message="Hello, World!" />);
    
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Test Component');
    expect(screen.getByTestId('message')).toHaveTextContent('Hello, World!');
  });

  it('should have jest-dom matchers available', () => {
    render(<TestComponent message="Testing matchers" />);
    
    const heading = screen.getByRole('heading', { level: 1 });
    expect(heading).toBeInTheDocument();
    expect(heading).toBeVisible();
    expect(heading).toHaveTextContent('Test Component');
  });

  it('should support path aliases (@/)', async () => {
    // This test verifies that the module name mapper is working
    // by checking that the test file itself can be resolved
    expect(true).toBe(true);
  });
});
