import type { Gauge } from 'lucide-react';
import type { DashboardData } from './dashboard-types';
export type Recommendation = DashboardData['analysis']['recommendations'][number];
export type IconComponent = typeof Gauge;
export type Delta = {
  text: string;
  direction: 'up' | 'down' | 'flat';
  tone: 'worse' | 'better' | 'neutral';
};
