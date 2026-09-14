import React from "react";
import type { JobView, ToolCard as Card } from "../api/client";
import type { CheckState } from "../useToolChecks";
import { TOOL_ORDER } from "../operational";
import { ActiveOperation } from "../components/ActiveOperation";
import { RecentHistory } from "../components/RecentHistory";
import { ToolCard } from "../components/ToolCard";

export function Overview({
  cards,
  history,
  lastFailures,
  updateBlocked,
  blockedReason,
  checkStates,
  planningId,
  activeJob,
  disconnected,
  onCheck,
  onPlan,
  onOpenJob,
  onViewHistory,
}: {
  cards: Card[];
  history: JobView[];
  lastFailures: Record<string, JobView>;
  updateBlocked: boolean;
  blockedReason: string;
  checkStates: Record<string, CheckState>;
  planningId: string;
  activeJob?: JobView;
  disconnected: boolean;
  onCheck: (toolId: string) => void;
  onPlan: (toolId: string) => void;
  onOpenJob: (jobId: string) => void;
  onViewHistory: () => void;
}): React.ReactElement {
  const sorted = [...cards].sort((a, b) => TOOL_ORDER.indexOf(a.id) - TOOL_ORDER.indexOf(b.id));
  return (
    <>
      {activeJob ? <ActiveOperation job={activeJob} onOpen={onOpenJob} /> : null}
      <section aria-label="Tool overview">
        <header className="section-head">
          <h2>Tools</h2>
          <p className="hint">
            {updateBlocked ? blockedReason : "Each card shows the last confirmed state and the next operator action."}
          </p>
        </header>
        <div className="card-grid">
          {sorted.map((c) => (
            <ToolCard
              key={c.id}
              card={c}
              updateBlocked={updateBlocked}
              blockedReason={blockedReason}
              checkState={checkStates[c.id]}
              lastFailure={lastFailures[c.id]}
              planning={planningId === c.id}
              disconnected={disconnected}
              onCheck={() => onCheck(c.id)}
              onPlan={() => onPlan(c.id)}
            />
          ))}
        </div>
      </section>
      <RecentHistory jobs={history} onOpen={onOpenJob} onViewAll={onViewHistory} />
    </>
  );
}
