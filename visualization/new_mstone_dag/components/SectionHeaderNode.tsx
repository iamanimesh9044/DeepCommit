
import React, { memo } from 'react';
import { NodeProps } from 'reactflow';
import { Grid } from 'lucide-react';

interface SectionHeaderData {
  label: string;
  subtitle?: string;
  compact?: boolean;
}

const SectionHeaderNode = ({ data }: NodeProps<SectionHeaderData>) => {
  const subtitle = data.subtitle || 'Milestones with no external dependencies';
  const compact = Boolean(data.compact);

  return (
    <div className="w-full border-b-4 border-slate-300 pb-4 mb-8 flex items-end gap-4 pointer-events-none">
        <div className={`bg-white border border-slate-200 rounded-lg text-slate-600 shadow-sm ${compact ? 'p-2' : 'p-3'}`}>
            <Grid size={compact ? 20 : 32} />
        </div>
        <div>
             {/* Changed text-slate-300 -> text-slate-700 for better visibility */}
             <h2 className={`${compact ? 'text-sm tracking-wide' : 'text-3xl tracking-widest'} font-black text-slate-700 uppercase leading-none`}>
                {data.label}
             </h2>
             {/* Changed text-slate-400 -> text-slate-500 */}
             <p className={`${compact ? 'text-[10px] mt-0.5' : 'text-sm mt-1'} text-slate-500 font-bold`}>
                {subtitle}
             </p>
        </div>
    </div>
  );
};

export default memo(SectionHeaderNode);
