
import React, { memo } from 'react';
import { Handle, Position, NodeProps } from 'reactflow';
import { Shield, Activity, Layers, AlertTriangle, PenTool, Database } from 'lucide-react';
import { Category, MilestoneData } from '../types';
import clsx from 'clsx';

const CategoryStyles: Record<Category, { bg: string; text: string; icon: React.ReactNode }> = {
  [Category.SECURITY_FIX]: { bg: 'bg-yellow-100', text: 'text-yellow-800', icon: <Shield size={14} /> },
  [Category.ARCHITECTURAL_REFACTOR]: { bg: 'bg-emerald-100', text: 'text-emerald-800', icon: <Layers size={14} /> },
  [Category.PLATFORM_SUPPORT]: { bg: 'bg-orange-100', text: 'text-orange-800', icon: <Database size={14} /> },
  [Category.BREAKING_CHANGE]: { bg: 'bg-teal-100', text: 'text-teal-800', icon: <AlertTriangle size={14} /> },
  [Category.MAJOR_FEATURE]: { bg: 'bg-blue-100', text: 'text-blue-800', icon: <Activity size={14} /> },
  [Category.MAINTENANCE]: { bg: 'bg-lime-100', text: 'text-lime-800', icon: <PenTool size={14} /> },
};

const GroupMilestoneNode = ({ data, selected }: NodeProps<MilestoneData>) => {
  const catStyle = CategoryStyles[data.category] || CategoryStyles[Category.MAINTENANCE];
  const integrationTestCommit = data.integrationTestCommit?.trim();
  const shortIntegrationTestCommit = integrationTestCommit
    ? integrationTestCommit.slice(0, 7)
    : null;

  return (
    <div
      className={clsx(
        "rounded-lg transition-all duration-200 relative",
        "bg-slate-50/30",
        selected ? "border-blue-500 shadow-xl border-2" : "border-slate-300 border-dashed border-2"
      )}
      style={{ width: '100%', height: '100%' }}
    >
      {/* Hidden handles for edge connections */}
      <Handle
        type="target"
        position={Position.Left}
        className="!bg-transparent !w-1 !h-1 !border-0"
      />

      {/* Group label in top-left corner */}
      <div className="absolute top-1 left-2 flex items-center gap-1.5 opacity-70 hover:opacity-100 transition-opacity">
        <span className="font-semibold text-slate-500 font-mono text-xs">{data.id}</span>
        <div className={clsx("flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[10px] font-bold uppercase", catStyle.bg, catStyle.text)}>
          {catStyle.icon}
        </div>
      </div>

      {shortIntegrationTestCommit && (
        <div
          className="absolute top-1 right-2 flex items-center gap-1 px-2 py-0.5 rounded-full bg-emerald-100 text-emerald-800 text-[10px] font-semibold border border-emerald-200 shadow-sm"
          title={`integration_test_commit: ${integrationTestCommit}`}
        >
          <span className="font-mono">integration_test_commit:</span>
          <span className="font-mono">{shortIntegrationTestCommit}</span>
        </div>
      )}

      <Handle
        type="source"
        position={Position.Right}
        className="!bg-transparent !w-1 !h-1 !border-0"
      />
    </div>
  );
};

export default memo(GroupMilestoneNode);
