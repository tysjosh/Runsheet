/**
 * PageHeader Component - Standardized page header
 * 
 * Provides consistent header layout across all pages.
 * Replaces 4 different header patterns with a single component.
 */

import React from 'react';
import { layout } from '../../styles/design-tokens';

export interface PageHeaderProps {
  title: string;
  subtitle?: string;
  icon?: React.ReactNode;
  actions?: React.ReactNode;
  badge?: React.ReactNode;
  className?: string;
}

export const PageHeader: React.FC<PageHeaderProps> = ({
  title,
  subtitle,
  icon,
  actions,
  badge,
  className = '',
}) => {
  return (
    <div className={`border-b border-gray-200 px-8 py-6 ${className}`}>
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          {icon && (
            <div className="w-10 h-10 bg-[#232323] rounded-xl flex items-center justify-center flex-shrink-0">
              {icon}
            </div>
          )}
          <div>
            <div className="flex items-center gap-3">
              <h1 className="text-2xl font-semibold text-[#232323]">{title}</h1>
              {badge}
            </div>
            {subtitle && <p className="text-gray-500 mt-1">{subtitle}</p>}
          </div>
        </div>
        {actions && <div className="flex items-center gap-3">{actions}</div>}
      </div>
    </div>
  );
};
