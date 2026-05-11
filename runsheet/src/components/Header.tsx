interface HeaderProps {
  onAIClick?: () => void;
}

export default function Header({ onAIClick }: HeaderProps) {
  return (
    <header
      className="relative overflow-hidden"
      style={{ backgroundColor: "var(--color-surface-muted)" }}
    >
      {/* Subtle gradient overlay */}
      <div
        className="absolute inset-0 opacity-50"
        style={{
          background:
            "linear-gradient(135deg, color-mix(in srgb, var(--color-surface) 80%, transparent) 0%, color-mix(in srgb, var(--color-gray-100) 40%, transparent) 100%)",
        }}
      />

      {/* Content */}
      <div className="relative px-8 py-3">
        <div className="max-w-7xl mx-auto flex items-center justify-between">
          {/* Logo/Brand */}
          <div className="flex items-center space-x-2.5">
            <div className="w-9 h-9 rounded-xl flex items-center justify-center bg-primary">
              <img
                src="/runsheet_logo.svg"
                alt="Runsheet Logo"
                className="w-5 h-5"
                style={{ filter: "brightness(0) invert(1)" }}
              />
            </div>
            <h1
              className="text-xl font-semibold tracking-tight"
              style={{ color: "var(--color-primary)" }}
            >
              Runsheet
            </h1>
          </div>

          {/* Header Actions */}
          <div className="flex items-center space-x-2">
            <button
              onClick={onAIClick}
              aria-label="Open AI support assistant"
              className="flex items-center space-x-2 px-4 py-2 rounded-lg transition-all duration-200 font-medium text-sm focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-gray-500"
              style={{
                color: "var(--color-primary)",
                backgroundColor:
                  "color-mix(in srgb, var(--color-surface) 80%, transparent)",
                border:
                  "1px solid color-mix(in srgb, var(--color-primary) 10%, transparent)",
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.backgroundColor = "var(--color-surface)";
                e.currentTarget.style.transform = "translateY(-1px)";
                e.currentTarget.style.boxShadow =
                  "0 4px 12px color-mix(in srgb, var(--color-ink) 8%, transparent)";
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.backgroundColor =
                  "color-mix(in srgb, var(--color-surface) 80%, transparent)";
                e.currentTarget.style.transform = "translateY(0)";
                e.currentTarget.style.boxShadow = "none";
              }}
            >
              <img
                src="/assistant.svg"
                alt="Support Assistant"
                className="w-5 h-5"
              />
              <span>Support</span>
            </button>
          </div>
        </div>
      </div>
    </header>
  );
}
