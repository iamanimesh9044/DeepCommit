
import React, { useState, useRef, useEffect, useCallback } from 'react';
import { X, Link as LinkIcon, FileText, Hash, Calendar, Info, Code, FileCode, ChevronDown, ChevronRight, TestTube, BookOpen } from 'lucide-react';
import { MilestoneData, DependencyData, Category } from '../types';
import { Node, Edge } from 'reactflow';
import clsx from 'clsx';

interface DetailPanelProps {
  selectedNode: Node<MilestoneData> | null;
  selectedEdge: Edge | null;
  onClose: () => void;
  allNodes: Node<MilestoneData>[];
  basePath: string | null;
}

type ImpactMetrics = {
  commits: number;
  additions: number;
  deletions: number;
  srcAdditions: number;
  srcDeletions: number;
  srcLoc: number;
};

/** Lightweight markdown → HTML (headings, bold, italic, code, lists, hr, paragraphs). */
function renderMarkdown(md: string): string {
  return md
    // code blocks (fenced)
    .replace(/```(\w*)\n([\s\S]*?)```/g, '<pre class="bg-gray-50 border border-gray-200 rounded-lg p-3 overflow-x-auto text-xs"><code>$2</code></pre>')
    // inline code
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    // headings
    .replace(/^#### (.+)$/gm, '<h4>$1</h4>')
    .replace(/^### (.+)$/gm, '<h3>$1</h3>')
    .replace(/^## (.+)$/gm, '<h2>$1</h2>')
    .replace(/^# (.+)$/gm, '<h1>$1</h1>')
    // horizontal rules
    .replace(/^---+$/gm, '<hr class="my-4 border-gray-200" />')
    // bold & italic
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    // unordered list items
    .replace(/^- (.+)$/gm, '<li>$1</li>')
    // wrap consecutive <li> in <ul>
    .replace(/((?:<li>.*<\/li>\n?)+)/g, '<ul class="list-disc pl-5 space-y-1">$1</ul>')
    // paragraphs — lines that aren't already HTML tags
    .replace(/^(?!<[a-z/])((?!\s*$).+)$/gm, '<p>$1</p>')
    // collapse blank lines
    .replace(/\n{3,}/g, '\n\n');
}

const normalizeCommit = (value?: string) => {
  const trimmed = value?.trim();
  return trimmed && trimmed.length > 0 ? trimmed : null;
};

const DetailPanel: React.FC<DetailPanelProps> = ({ selectedNode, selectedEdge, onClose, allNodes, basePath }) => {
  const titleRef = useRef<HTMLHeadingElement>(null);
  const [isTruncated, setIsTruncated] = useState(false);
  const [srsContent, setSrsContent] = useState<string | null>(null);
  const [srsLoading, setSrsLoading] = useState(false);
  const [srsError, setSrsError] = useState<string | null>(null);
  const [showSrsModal, setShowSrsModal] = useState(false);

  useEffect(() => {
    if (titleRef.current) {
      setIsTruncated(titleRef.current.scrollWidth > titleRef.current.clientWidth);
    }
  }, [selectedNode?.id]);

  // Reset SRS state when node changes
  useEffect(() => {
    setSrsContent(null);
    setSrsError(null);
    setShowSrsModal(false);
  }, [selectedNode?.id]);

  const fetchSrs = useCallback(async () => {
    if (!selectedNode || !basePath) return;
    setSrsLoading(true);
    setSrsError(null);
    try {
      const url = `${basePath}/srs/${selectedNode.id}/SRS.md`;
      const res = await fetch(url);
      if (!res.ok) throw new Error(res.status === 404 ? 'SRS not found for this milestone' : `Failed to load SRS (${res.status})`);
      const text = await res.text();
      setSrsContent(text);
      setShowSrsModal(true);
    } catch (err) {
      setSrsError(err instanceof Error ? err.message : 'Failed to load SRS');
      setShowSrsModal(true);
    } finally {
      setSrsLoading(false);
    }
  }, [selectedNode, basePath]);

  if (!selectedNode && !selectedEdge) return null;

  const isGroupNode = Boolean(selectedNode && selectedNode.type === 'groupMilestone');
  const childNodes = selectedNode && isGroupNode
    ? allNodes.filter((node) => node.parentNode === selectedNode.id)
    : [];
  const hasChildren = childNodes.length > 0;

  const impactMetrics: ImpactMetrics | null = selectedNode
    ? (hasChildren
        ? childNodes.reduce<ImpactMetrics>((acc, node) => {
            const data = node.data;
            acc.commits += data.commits || 0;
            acc.additions += data.additions || 0;
            acc.deletions += data.deletions || 0;
            acc.srcAdditions += data.srcAdditions || 0;
            acc.srcDeletions += data.srcDeletions || 0;
            acc.srcLoc += data.srcLoc || 0;
            return acc;
          }, { commits: 0, additions: 0, deletions: 0, srcAdditions: 0, srcDeletions: 0, srcLoc: 0 })
        : {
            commits: selectedNode.data.commits,
            additions: selectedNode.data.additions,
            deletions: selectedNode.data.deletions,
            srcAdditions: selectedNode.data.srcAdditions,
            srcDeletions: selectedNode.data.srcDeletions,
            srcLoc: selectedNode.data.srcLoc,
          })
    : null;

  const integrationTestCommits = selectedNode ? (() => {
    const commits: string[] = [];
    const seen = new Set<string>();
    const pushCommit = (value?: string | null) => {
      const normalized = normalizeCommit(value || undefined);
      if (normalized && !seen.has(normalized)) {
        seen.add(normalized);
        commits.push(normalized);
      }
    };

    pushCommit(selectedNode.data.integrationTestCommit);
    if (hasChildren) {
      childNodes.forEach((node) => pushCommit(node.data.integrationTestCommit));
    }
    return commits;
  })() : [];

  const commitHashesForDisplay = selectedNode ? (() => {
    const commits: string[] = [];
    const seen = new Set<string>();
    const pushCommit = (value?: string | null) => {
      const normalized = normalizeCommit(value || undefined);
      if (normalized && !seen.has(normalized)) {
        seen.add(normalized);
        commits.push(normalized);
      }
    };

    selectedNode.data.commitHashes.forEach((hash) => pushCommit(hash));
    if (hasChildren) {
      childNodes.forEach((node) => node.data.commitHashes.forEach((hash) => pushCommit(hash)));
    }
    integrationTestCommits.forEach((hash) => pushCommit(hash));
    return commits;
  })() : [];

  const isIntegrationCommit = (hash: string) => {
    const normalizedHash = normalizeCommit(hash);
    if (!normalizedHash) return false;
    return integrationTestCommits.some((integration) =>
      integration === normalizedHash
      || integration.startsWith(normalizedHash)
      || normalizedHash.startsWith(integration)
    );
  };

  return (
    <div className="absolute top-20 right-4 w-[420px] bg-white rounded-xl shadow-2xl border border-gray-200 z-50 flex flex-col max-h-[85vh] overflow-hidden transition-all duration-300 animate-[slideIn_0.3s_ease-out]">
      <style>{`
        @keyframes slideIn {
          from { opacity: 0; transform: translateX(20px); }
          to { opacity: 1; transform: translateX(0); }
        }
      `}</style>

      {/* Header */}
      <div className="border-b border-gray-200 bg-gradient-to-r from-slate-50 to-gray-50">
        <div className={clsx("px-4 pt-4 flex justify-between items-center", isTruncated ? "pb-2" : "pb-4")}>
          <h2 ref={titleRef} className="font-bold text-lg text-gray-800 truncate max-w-[340px]" title={selectedNode ? selectedNode.id : 'Edge Details'}>
            {selectedNode ? selectedNode.id : 'Edge Details'}
          </h2>
          <button onClick={onClose} className="p-1.5 hover:bg-white/80 rounded-full transition-colors">
            <X size={18} className="text-gray-400 hover:text-gray-600" />
          </button>
        </div>
        {selectedNode && isTruncated && (
          <div className="px-4 pb-3">
            <div className="px-3 py-2 bg-white rounded-lg text-xs font-mono text-slate-600 break-all select-all border border-slate-200/80 shadow-sm">
              {selectedNode.id}
            </div>
          </div>
        )}
      </div>

      <div className="p-5 overflow-y-auto flex-1 scrollbar-thin scrollbar-thumb-gray-200">
        {/* Node Details */}
        {selectedNode && (
          <div className="space-y-6">
            <div>
              <h3 className="font-bold text-xl text-gray-900 mb-2 leading-snug">{selectedNode.data.label}</h3>
              <div className="flex items-center gap-2 flex-wrap">
                <div className="inline-flex items-center gap-2 px-3 py-1 bg-gray-100 rounded-full text-xs font-semibold text-gray-600">
                  <span className={clsx("w-2 h-2 rounded-full",
                     selectedNode.data.category === Category.SECURITY_FIX ? 'bg-yellow-500' : 'bg-blue-500'
                  )}></span>
                  {selectedNode.data.category}
                </div>
                {basePath && (
                  <button
                    onClick={fetchSrs}
                    disabled={srsLoading}
                    className="inline-flex items-center gap-1.5 px-3 py-1 bg-indigo-50 hover:bg-indigo-100 border border-indigo-200 rounded-full text-xs font-semibold text-indigo-600 transition-colors disabled:opacity-50"
                  >
                    <BookOpen size={12} />
                    {srsLoading ? 'Loading...' : 'View SRS'}
                  </button>
                )}
              </div>
            </div>

            {/* Mini-SRS / Rationale */}
            <div className="space-y-3">
              <SectionTitle icon={<Info size={16} />} title="Description" />
              <div className="p-3 bg-amber-50/50 rounded-lg text-sm text-gray-800 leading-relaxed border border-amber-100/50 shadow-sm">
                {selectedNode.data.miniSrs || selectedNode.data.description}
              </div>
            </div>

            {/* Detailed Stats Grid */}
            <div className="space-y-3">
              <SectionTitle icon={<ActivityIcon size={16} />} title="Impact Metrics" />
              <div className="grid grid-cols-3 gap-3">
                  <StatBox label="Commits" value={impactMetrics?.commits ?? 0} icon={<Hash />} />
                  <StatBox 
                    label="Total Chg" 
                    value={`+${impactMetrics?.additions ?? 0} / -${impactMetrics?.deletions ?? 0}`} 
                    icon={<FileText />} 
                    subValue={`${(impactMetrics?.additions ?? 0) + (impactMetrics?.deletions ?? 0)} LoC`}
                  />
                  <StatBox label="Src Size" value={impactMetrics?.srcLoc ?? 0} icon={<Code />} subValue="Current LoC" />
               </div>
               <div className="grid grid-cols-2 gap-3 mt-2">
                  <div className="bg-emerald-50 border border-emerald-100 rounded p-2 flex items-center justify-between">
                     <span className="text-xs font-bold text-emerald-700 uppercase">Src Added</span>
                     <span className="font-mono font-bold text-emerald-600">+{impactMetrics?.srcAdditions ?? 0}</span>
                  </div>
                  <div className="bg-red-50 border border-red-100 rounded p-2 flex items-center justify-between">
                     <span className="text-xs font-bold text-red-700 uppercase">Src Deleted</span>
                     <span className="font-mono font-bold text-red-600">-{impactMetrics?.srcDeletions ?? 0}</span>
                  </div>
               </div>
            </div>

            <div className="grid grid-cols-2 gap-3">
                <StatBox label="Start" value={selectedNode.data.startDate} icon={<Calendar />} />
                <StatBox label="End" value={selectedNode.data.endDate} icon={<Calendar />} />
            </div>

            {/* Collapsible Files Sections */}
            <div className="space-y-4 pt-2">
              <CollapsibleSection 
                title="Touched Source Files" 
                count={selectedNode.data.touchedSrcFiles.length} 
                icon={<FileCode size={16} className="text-blue-500"/>}
              >
                 <ul className="space-y-1">
                  {selectedNode.data.touchedSrcFiles.map((file, idx) => (
                    <li key={idx} className="text-xs font-mono text-slate-600 break-all pl-2 border-l-2 border-slate-200">
                      {file}
                    </li>
                  ))}
                 </ul>
              </CollapsibleSection>

              <CollapsibleSection 
                title="Touched Test Files" 
                count={selectedNode.data.touchedTestFiles.length}
                icon={<TestTube size={16} className="text-green-500"/>}
              >
                 <ul className="space-y-1">
                  {selectedNode.data.touchedTestFiles.map((file, idx) => (
                    <li key={idx} className="text-xs font-mono text-slate-600 break-all pl-2 border-l-2 border-green-200">
                      {file}
                    </li>
                  ))}
                 </ul>
              </CollapsibleSection>
            </div>

            {/* Commit Hashes */}
            {commitHashesForDisplay.length > 0 && (
                <div className="space-y-3 pt-2">
                <SectionTitle icon={<LinkIcon size={16} />} title="Commits" />
                <div className="flex flex-wrap gap-2">
                    {commitHashesForDisplay.map((hash) => (
                    <span
                      key={hash}
                      className={clsx(
                        "px-2 py-1 border rounded text-[10px] font-mono cursor-default",
                        isIntegrationCommit(hash)
                          ? "bg-emerald-100 text-emerald-800 border-emerald-200 font-semibold"
                          : "bg-slate-100 text-slate-600 border-slate-200 hover:bg-slate-200"
                      )}
                      title={isIntegrationCommit(hash) ? "integration_test_commit" : undefined}
                    >
                      {hash}
                    </span>
                    ))}
                </div>
                </div>
            )}
          </div>
        )}

        {/* SRS Modal */}
        {showSrsModal && (
          <div
            className="fixed inset-0 z-[100] flex items-center justify-center bg-black/40 backdrop-blur-sm"
            onClick={() => setShowSrsModal(false)}
          >
            <div
              className="bg-white rounded-xl shadow-2xl border border-gray-200 w-[720px] max-w-[90vw] max-h-[85vh] flex flex-col animate-[slideIn_0.2s_ease-out]"
              onClick={(e) => e.stopPropagation()}
            >
              {/* Modal Header */}
              <div className="flex items-center justify-between px-5 py-3 border-b border-gray-200 bg-gradient-to-r from-indigo-50 to-slate-50 rounded-t-xl">
                <div className="flex items-center gap-2">
                  <BookOpen size={16} className="text-indigo-500" />
                  <span className="font-bold text-sm text-gray-800">Software Requirements Specification</span>
                  <span className="text-xs text-gray-400 font-mono">{selectedNode?.id}</span>
                </div>
                <button
                  onClick={() => setShowSrsModal(false)}
                  className="p-1.5 hover:bg-white/80 rounded-full transition-colors"
                >
                  <X size={16} className="text-gray-400 hover:text-gray-600" />
                </button>
              </div>
              {/* Modal Body */}
              <div className="p-5 overflow-y-auto flex-1">
                {srsError ? (
                  <div className="text-center py-8 text-red-500 text-sm">{srsError}</div>
                ) : srsContent ? (
                  <div
                    className="srs-content text-sm text-gray-700 leading-relaxed [&_h1]:text-xl [&_h1]:font-bold [&_h1]:text-gray-900 [&_h1]:mt-4 [&_h1]:mb-2 [&_h2]:text-lg [&_h2]:font-bold [&_h2]:text-gray-800 [&_h2]:mt-4 [&_h2]:mb-2 [&_h3]:text-base [&_h3]:font-semibold [&_h3]:text-gray-800 [&_h3]:mt-3 [&_h3]:mb-1 [&_h4]:text-sm [&_h4]:font-semibold [&_h4]:text-gray-700 [&_h4]:mt-2 [&_h4]:mb-1 [&_ul]:list-disc [&_ul]:pl-5 [&_ul]:space-y-1 [&_p]:mb-2 [&_code]:bg-gray-100 [&_code]:px-1 [&_code]:rounded [&_code]:text-xs [&_code]:font-mono [&_strong]:text-gray-800"
                    dangerouslySetInnerHTML={{ __html: renderMarkdown(srsContent) }}
                  />
                ) : null}
              </div>
            </div>
          </div>
        )}

        {/* Edge Details */}
        {selectedEdge && (
          <div className="space-y-6">
             <div>
              <h3 className="font-bold text-xl text-gray-900 mb-1">Dependency Relationship</h3>
              <p className="text-sm text-gray-500 font-mono">
                {selectedEdge.source} <span className="text-gray-300">➔</span> {selectedEdge.target}
              </p>
            </div>

            <div className="p-4 bg-blue-50 border border-blue-100 rounded-lg space-y-2">
               <div className="flex justify-between items-center">
                 <span className="text-sm font-medium text-blue-700">Type</span>
                 <span className="text-sm font-bold text-blue-900">{(selectedEdge.data as DependencyData)?.type || 'N/A'}</span>
               </div>
               <div className="flex justify-between items-center">
                 <span className="text-sm font-medium text-blue-700">Strength</span>
                 <span className="text-sm font-bold text-blue-900 uppercase">{(selectedEdge.data as DependencyData)?.strength || 'N/A'}</span>
               </div>
            </div>

            <div className="space-y-2">
              <SectionTitle icon={<Info size={16} />} title="Rationale" />
              <div className="p-3 bg-gray-50 text-gray-700 text-sm rounded border border-gray-200 whitespace-pre-line leading-relaxed">
                {(selectedEdge.data as DependencyData)?.description || (selectedEdge.data as any)?.rationale || "No specific rationale provided."}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
};

// --- Sub Components ---

const SectionTitle = ({ icon, title }: { icon: React.ReactNode; title: string }) => (
  <h4 className="flex items-center gap-2 text-xs font-extrabold text-gray-400 uppercase tracking-widest">
    {icon} {title}
  </h4>
);

const ActivityIcon = ({size}: {size: number}) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"></polyline>
    </svg>
);

const StatBox = ({ label, value, icon, subValue }: { label: string; value: string | number; icon: React.ReactNode; subValue?: string }) => (
  <div className="bg-white p-2 rounded-lg border border-gray-200 shadow-sm flex flex-col items-center text-center">
    <div className="text-[10px] text-gray-400 font-bold flex items-center gap-1 uppercase tracking-tight mb-1">
      {React.isValidElement(icon) ? React.cloneElement(icon as React.ReactElement<any>, { size: 12 }) : icon}
      {label}
    </div>
    <div className="text-sm font-bold text-gray-800 leading-tight">{value}</div>
    {subValue && <div className="text-[10px] text-gray-400 mt-0.5">{subValue}</div>}
  </div>
);

const CollapsibleSection = ({ title, count, icon, children }: { title: string; count: number; icon: React.ReactNode; children: React.ReactNode }) => {
    const [isOpen, setIsOpen] = useState(false);

    if (count === 0) return null;

    return (
        <div className="border border-gray-200 rounded-lg overflow-hidden">
            <button 
                onClick={() => setIsOpen(!isOpen)}
                className="w-full flex items-center justify-between p-3 bg-gray-50 hover:bg-gray-100 transition-colors"
            >
                <div className="flex items-center gap-2 text-sm font-semibold text-gray-700">
                    {icon}
                    <span>{title}</span>
                    <span className="bg-gray-200 text-gray-600 px-1.5 py-0.5 rounded-full text-[10px] font-bold">{count}</span>
                </div>
                {isOpen ? <ChevronDown size={16} className="text-gray-400" /> : <ChevronRight size={16} className="text-gray-400" />}
            </button>
            {isOpen && (
                <div className="p-3 bg-white border-t border-gray-100 max-h-[200px] overflow-y-auto shadow-inner">
                    {children}
                </div>
            )}
        </div>
    );
}

export default DetailPanel;
