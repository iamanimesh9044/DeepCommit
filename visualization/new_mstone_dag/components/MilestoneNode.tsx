
import React, { memo } from 'react';
import { Handle, Position, NodeProps } from 'reactflow';
import { FileCode, GitCommit, Calendar, Shield, Activity, Layers, AlertTriangle, PenTool, Database } from 'lucide-react';
import { Category, MilestoneData } from '../types';
import clsx from 'clsx';

const CategoryStyles: Record<Category, { bg: string; text: string; icon: React.ReactNode }> = {
  [Category.SECURITY_FIX]: { bg: 'bg-yellow-200', text: 'text-yellow-800', icon: <Shield size={12} /> },
  [Category.ARCHITECTURAL_REFACTOR]: { bg: 'bg-emerald-200', text: 'text-emerald-800', icon: <Layers size={12} /> },
  [Category.PLATFORM_SUPPORT]: { bg: 'bg-orange-200', text: 'text-orange-800', icon: <Database size={12} /> },
  [Category.BREAKING_CHANGE]: { bg: 'bg-teal-200', text: 'text-teal-800', icon: <AlertTriangle size={12} /> },
  [Category.MAJOR_FEATURE]: { bg: 'bg-blue-200', text: 'text-blue-800', icon: <Activity size={12} /> },
  [Category.MAINTENANCE]: { bg: 'bg-lime-200', text: 'text-lime-800', icon: <PenTool size={12} /> },
};

// Truncate ID for card display, full ID shown on hover
const truncateId = (id: string, maxLength: number = 26): string => {
  if (id.length <= maxLength) return id;
  return id.substring(0, maxLength - 3) + '...';
};

const MilestoneNode = ({ data, selected }: NodeProps<MilestoneData>) => {
  const style = CategoryStyles[data.category] || CategoryStyles[Category.MAINTENANCE];
  const truncatedId = truncateId(data.id, 26);

  return (
    <div className={clsx(
      "w-[405px] h-[130px] rounded-lg shadow-md border transition-all duration-200 relative flex flex-col",
      selected
        ? "bg-gray-50 border-blue-500 shadow-xl ring-2 ring-blue-200"
        : "bg-gray-50 border-gray-300 hover:border-gray-400"
    )}>
      <Handle type="target" position={Position.Left} className="!bg-gray-400 !w-2 !h-2" />
      
      {/* Header */}
      <div className="flex justify-between items-center px-3 py-2 border-b border-gray-200 bg-gray-100/50 rounded-t-lg">
        <span className="font-bold text-gray-700 font-mono text-sm" title={data.id}>
          {truncatedId}
        </span>
        <div className="flex items-center gap-1.5">
          {typeof data.topoLayer === 'number' && (
            <div
              className="px-1.5 py-0.5 rounded-md text-[10px] font-bold text-indigo-700 bg-indigo-100 border border-indigo-200"
              title={`Unlock layer ${data.topoLayer}`}
            >
              L{data.topoLayer}
            </div>
          )}
          <div
            className={clsx(
              "flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-bold uppercase tracking-wide",
              style.bg,
              style.text
            )}
          >
            {style.icon}
            <span>{data.category}</span>
          </div>
        </div>
      </div>

      {/* Body */}
      <div className="px-4 py-2 bg-white flex-1 flex items-center overflow-hidden">
        <h3 className="font-semibold text-gray-800 text-sm leading-tight line-clamp-2">
          {data.label}
        </h3>
      </div>

      {/* Footer / Metrics */}
      <div className="px-3 py-2 bg-gray-50 border-t border-gray-200 grid grid-cols-3 gap-2 text-[10px] text-gray-500 rounded-b-lg relative">
        <div className="flex items-center gap-1.5" title="Commits">
          <GitCommit size={12} className="text-amber-500" />
          <span>{data.commits} commits</span>
        </div>
        <div className="flex items-center gap-1.5" title="Lines of Code">
          <FileCode size={12} className="text-blue-500" />
          <span>src LoC: {data.srcLoc}</span>
        </div>
        <div className="flex items-center gap-1.5" title="Date Range">
          <Calendar size={12} className="text-red-500" />
          <span>{data.startDate}-{data.endDate}</span>
        </div>
      </div>

      <Handle type="source" position={Position.Right} className="!bg-gray-400 !w-2 !h-2" />
      
      {/* Special Bottom Handles for Manual Routing */}
      <Handle 
        type="source" 
        position={Position.Bottom} 
        id="bottom-source"
        className="!bg-gray-400 !w-2 !h-2"
        style={{ left: '65%' }} 
      />
      <Handle 
        type="target" 
        position={Position.Bottom} 
        id="bottom-target"
        className="!bg-gray-400 !w-2 !h-2"
        style={{ left: '35%' }} 
      />
    </div>
  );
};

export default memo(MilestoneNode);
