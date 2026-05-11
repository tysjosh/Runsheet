/**
 * StatsBar Component - Standardized statistics display
 * 
 * Provides consistent stats layout with two variants:
 * - inline: Compact horizontal layout
 * - grid: Dashboard-style grid cards
 */

import React from 'react';

export interface Stat {
  label: string;
  value: string | number;
  color?: 'default' | 'success' | 'error' | 'warning' | 'info';
  icon?: React.ReactNode;
}

export interface StatsBarProps {
  stats: Stat[];
  variant?: 'inline' | 'grid';
  columns?: number;
  className?: string;
}

const colorStyles = {
  default: 'text-gray-900',
  success: 'text-green-600',
  error: 'text-red-600',
  warning: 'text-yellow-600',
  info: 'text-blue-600',
};

export const StatsBar: React.FC<StatsBarProps> = ({
  stats,
  variant = 'grid',
  columns = 4,
  className = '',
}) => {
  if (variant === 'inline') {
    return (
      <div className={`flex gap-6 flex-wrap ${className}`}>
        {stats.map((stat, index) => (
          <div key={index} className="flex items-center gap-2">
            {stat.icon}
            <span className="text-sm text-gray-600">{stat.label}:</span>
            <span className={`text-sm font-semibold ${colorStyles[stat.color || 'default']}`}>
              {stat.value}
            </span>
          </div>
        ))}
      </div>
    );
  }

  return (
    <div className={`grid gap-6 ${className}`} style={{ gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` }}>
      {stats.map((stat, index) => (
        <div key={index} className="text-center">
          {stat.icon && <div className="flex justify-center mb-2">{stat.icon}</div>}
          <div className={`text-2xl font-semibold ${colorStyles[stat.color || 'default']}`}>
            {stat.value}
          </div>
          <div className="text-sm text-gray-500 mt-1">{stat.label}</div>
        </div>
      ))}
    </div>
  );
};
